import os
import re
import traceback

try:
    import cPickle as pickle
except ImportError:
    import pickle

from functools import partial

import click

from sqlalchemy.exc import OperationalError, ArgumentError
from sqlalchemy.engine.url import _parse_rfc1738_args as parse_engine_uri
from brainpy import periodic_table
from ms_deisotope.averagine import (
    Averagine, glycan as n_glycan_averagine, permethylated_glycan,
    peptide, glycopeptide, heparin, heparan_sulfate)

from glycan_profiling.serialize import (
    DatabaseBoundOperation, GlycanHypothesis, GlycopeptideHypothesis,
    SampleRun, Analysis, AnalysisTypeEnum)

from glycan_profiling.database.builder.glycan import (
    GlycanCompositionHypothesisMerger,
    named_reductions, named_derivatizations)

from glycan_profiling.database.builder.glycan.synthesis import synthesis_register
from glycan_profiling.database.builder.glycopeptide.proteomics import mzid_proteome
from glycan_profiling.chromatogram_tree import (
    MassShift, Formate, Ammonium,
    Sodium, Potassium)

from glycan_profiling.tandem.glycopeptide.scoring import (
    binomial_score, simple_score, coverage_weighted_binomial, intensity_scorer,
    SpectrumMatcherBase)

from glycan_profiling.models import ms1_model_features

from glycopeptidepy.utils.collectiontools import decoratordict
from glycopeptidepy.structure.modification import ModificationTable

from glypy import Substituent, Composition

glycan_source_validators = decoratordict()


class ModificationValidator(object):
    """Determines whether a provided command line argument can be mapped to a
    valid peptide modification in a default contructed
    :class:`glycopeptidepy.structure.modification.ModificationTable`

    Attributes
    ----------
    table : glycopeptidepy.structure.modification.ModificationTable
        The modification database to look up modifications by name.
    """
    def __init__(self):
        self.table = ModificationTable()

    def validate(self, modification_string):
        try:
            return self.table[modification_string]
        except KeyError:
            return False


class GlycanSourceValidatorBase(DatabaseBoundOperation):
    def __init__(self, database_connection, source, source_type, source_identifier=None):
        DatabaseBoundOperation.__init__(self, database_connection)
        self.source = source
        self.source_type = source_type
        self.source_identifier = source_identifier

    def validate(self):
        raise NotImplementedError()


@glycan_source_validators('text')
@glycan_source_validators('combinatorial')
class TextGlycanSourceValidator(GlycanSourceValidatorBase):
    def __init__(self, database_connection, source, source_type, source_identifier=None):
        super(TextGlycanSourceValidator, self).__init__(
            database_connection, source, source_type, source_identifier)

    def validate(self):
        return os.path.exists(self.source)


@glycan_source_validators("hypothesis")
class HypothesisGlycanSourceValidator(GlycanSourceValidatorBase):
    def __init__(self, database_connection, source, source_type, source_identifier=None):
        super(HypothesisGlycanSourceValidator, self).__init__(
            database_connection, source, source_type, source_identifier)
        self.handle = DatabaseBoundOperation(source)

    def validate(self):
        if self.source_identifier is None:
            click.secho("No value passed through --glycan-source-identifier.", fg='magenta')
            return False
        try:
            hypothesis_id = int(self.source_identifier)
            inst = self.handle.query(GlycanHypothesis).get(hypothesis_id)
            return inst is not None
        except TypeError:
            hypothesis_name = self.source
            inst = self.handle.query(GlycanHypothesis).filter(GlycanHypothesis.name == hypothesis_name).first()
            return inst is not None


@glycan_source_validators("analysis")
class GlycanAnalysisGlycanSourceValidator(GlycanSourceValidatorBase):
    def __init__(self, database_connection, source, source_type, source_identifier=None):
        super(GlycanAnalysisGlycanSourceValidator, self).__init__(
            database_connection, source, source_type, source_identifier)
        self.handle = DatabaseBoundOperation(source)

    def validate(self):
        if self.source_identifier is None:
            click.secho("No value passed through --glycan-source-identifier.", fg='magenta')
            return False
        try:
            analysis_id = int(self.source_identifier)
            inst = self.handle.query(Analysis).filter(
                Analysis.id == analysis_id,
                Analysis.analysis_type == AnalysisTypeEnum.glycan_lc_ms.name)
            return inst is not None
        except TypeError:
            hypothesis_name = self.source
            inst = self.handle.query(Analysis).filter(
                Analysis.name == hypothesis_name,
                Analysis.analysis_type == AnalysisTypeEnum.glycan_lc_ms.name).first()
            return inst is not None


class GlycanHypothesisCopier(GlycanCompositionHypothesisMerger):
    pass


def validate_glycan_source(context, database_connection, source, source_type, source_identifier=None):
    glycan_source_validator_type = glycan_source_validators[source_type]
    glycan_validator = glycan_source_validator_type(database_connection, source, source_type, source_identifier)
    if not glycan_validator.validate():
        click.secho("Could not validate glycan source %s of type %s" % (
            source, source_type), fg='yellow')
        raise click.Abort()


def validate_modifications(context, modifications):
    mod_validator = ModificationValidator()
    for mod in modifications:
        if not mod_validator.validate(mod):
            click.secho("Invalid modification '%s'" % mod, fg='yellow')
            raise click.Abort()


def validate_unique_name(context, database_connection, name, klass):
    handle = DatabaseBoundOperation(database_connection)
    obj = handle.query(klass).filter(
        klass.name == name).first()
    if obj is not None:
        return klass.make_unique_name(handle.session, name)
    else:
        return name


validate_glycopeptide_hypothesis_name = partial(validate_unique_name, klass=GlycopeptideHypothesis)
validate_glycan_hypothesis_name = partial(validate_unique_name, klass=GlycanHypothesis)
validate_sample_run_name = partial(validate_unique_name, klass=SampleRun)
validate_analysis_name = partial(validate_unique_name, klass=Analysis)


def _resolve_protein_name_list(context, args):
    result = []
    for arg in args:
        if isinstance(arg, basestring):
            if os.path.exists(arg) and os.path.isfile(arg):
                with open(arg) as fh:
                    for line in fh:
                        cleaned = line.strip()
                        if cleaned:
                            result.append(cleaned)
            else:
                result.append(arg)
        else:
            if isinstance(arg, (list, tuple)):
                result.extend(arg)
            else:
                result.append(arg)
    return result


def validate_mzid_proteins(context, mzid_file, target_proteins=tuple(), target_proteins_re=tuple()):
    all_proteins = set(mzid_proteome.protein_names(mzid_file))
    accepted_target_proteins = set()
    target_proteins = _resolve_protein_name_list(context, target_proteins)
    target_proteins_re = _resolve_protein_name_list(context, target_proteins_re)
    for prot in target_proteins:
        if prot in all_proteins:
            accepted_target_proteins.add(prot)
        else:
            click.secho("Could not find protein '%s'" % prot, fg='yellow')
    for prot_re in target_proteins_re:
        pat = re.compile(prot_re)
        hits = 0
        for prot in all_proteins:
            if pat.search(prot):
                accepted_target_proteins.add(prot)
                hits += 1

        if hits == 0:
            click.secho("Pattern '%s' did not match any proteins" % prot_re, fg='yellow')
    if len(accepted_target_proteins) == 0:
        click.secho("Using all proteins", fg='yellow')
    return list(accepted_target_proteins)


def validate_reduction(context, reduction_string):
    if reduction_string is None:
        return None
    try:
        if str(reduction_string).lower() in named_reductions:
            return named_reductions[str(reduction_string).lower()]
        else:
            if len(Composition(str(reduction_string))) > 0:
                return str(reduction_string)
            else:
                raise Exception("Invalid")
    except Exception:
        click.secho("Could not validate reduction '%s'" % reduction_string)
        raise click.Abort("Could not validate reduction '%s'" % reduction_string)


def validate_derivatization(context, derivatization_string):
    if derivatization_string is None:
        return derivatization_string
    if derivatization_string in named_derivatizations:
        return named_derivatizations[derivatization_string]
    subst = Substituent(derivatization_string)
    if len(subst.composition) == 0:
        click.secho("Could not validate derivatization '%s'" % derivatization_string)
        raise click.Abort("Could not validate derivatization '%s'" % derivatization_string)
    else:
        return derivatization_string


def validate_element(element):
    valid = element in periodic_table
    if not valid:
        raise click.Abort("%r is not a valid element" % element)
    return valid


def parse_averagine_formula(formula):
    if isinstance(formula, Averagine):
        return formula
    return Averagine({k: float(v) for k, v in re.findall(r"([A-Z][a-z]*)([0-9\.]*)", formula)
                      if float(v or 0) > 0 and validate_element(k)})


averagines = {
    'glycan': n_glycan_averagine,
    'permethylated-glycan': permethylated_glycan,
    'peptide': peptide,
    'glycopeptide': glycopeptide,
    'heparin': heparin,
    "heparan-sulfate": heparan_sulfate
}


def validate_averagine(averagine_string):
    if isinstance(averagine_string, Averagine):
        return averagine_string
    if averagine_string in averagines:
        return averagines[averagine_string]
    else:
        return parse_averagine_formula(averagine_string)


class AveragineParamType(click.types.StringParamType):
    name = "MODEL"

    models = averagines

    def convert(self, value, param, ctx):
        return validate_averagine(value)

    def get_metavar(self, param):
        return '[%s]' % '|'.join(sorted(averagines.keys()))

    def get_missing_message(self, param):
        return 'Choose from %s, or provide a formula.' % ', '.join(self.choices)


class SubstituentParamType(click.types.StringParamType):
    name = "SUBSTITUENT"

    def convert(self, value, param, ctx):
        t = Substituent(value)
        if not t.composition:
            raise ValueError("%s is not a recognized substituent" % value)
        return t


mass_shifts = {
    "ammonium": Ammonium,
    "formate": Formate,
    "sodium": Sodium,
    "potassium": Potassium,
}


def validate_mass_shift(mass_shift_string, multiplicity=1):
    multiplicity = int(multiplicity)
    if mass_shift_string.lower() in mass_shifts:
        return (mass_shifts[mass_shift_string.lower()], multiplicity)
    else:
        try:
            mass_shift_string = str(mass_shift_string)
            composition = Composition(mass_shift_string)
            shift = MassShift(mass_shift_string, composition)
            return (shift, multiplicity)
        except Exception as e:
            click.secho("%r" % (e,))
            click.secho("Could not validate mass_shift %r" % (mass_shift_string,), fg='yellow')
            raise click.Abort("Could not validate mass_shift %r" % (mass_shift_string,))


glycopeptide_tandem_scoring_functions = {
    "binomial": binomial_score.BinomialSpectrumMatcher,
    "simple": simple_score.SimpleCoverageScorer,
    "coverage_weighted_binomial": coverage_weighted_binomial.CoverageWeightedBinomialScorer,
    "log_intensity": intensity_scorer.LogIntensityScorer,
    "penalized_log_intensty": intensity_scorer.FullSignaturePenalizedLogIntensityScorer,
}


def validate_glycopeptide_tandem_scoring_function(context, name):
    if isinstance(name, SpectrumMatcherBase):
        return name
    try:
        return glycopeptide_tandem_scoring_functions[name]
    except KeyError:
        # Custom scorer loading
        if name.startswith("model://"):
            path = name[8:]
            if not os.path.exists(path):
                raise click.Abort("Model file at \"%s\" does not exist." % (path, ))
            with open(path, 'rb') as fh:
                scorer = pickle.load(fh)
                return scorer
        else:
            raise click.Abort("Could not recognize scoring function by name %r" % (name,))


class ChoiceOrURI(click.Choice):
    def get_metavar(self, param):
        return "[%s|model://path]" % ('|'.join(self.choices), )

    def get_missing_message(self, param):
        choice_str = ",\n\t".join(self.choices)
        return "Choose from:\n\t%s\nor specify a model://path" % choice_str

    def convert(self, value, param, ctx):
        # Match through normalization and case sensitivity
        # first do token_normalize_func, then lowercase
        # preserve original `value` to produce an accurate message in
        # `self.fail`
        normed_value = value
        normed_choices = {choice: choice for choice in self.choices}

        if ctx is not None and ctx.token_normalize_func is not None:
            normed_value = ctx.token_normalize_func(value)
            normed_choices = {
                ctx.token_normalize_func(normed_choice): original
                for normed_choice, original in normed_choices.items()
            }

        if not self.case_sensitive:
            normed_value = normed_value.casefold()
            normed_choices = {
                normed_choice.casefold(): original
                for normed_choice, original in normed_choices.items()
            }

        if normed_value in normed_choices:
            return normed_choices[normed_value]

        if value.startswith("model://"):
            return value

        self.fail(
            "invalid value: {value}. (choose from {choices} or specify a \"model://path\")".format(
                value=value, choices=', '.join(self.choices)),
            param,
            ctx,
        )


def get_by_name_or_id(session, model_type, name_or_id):
    try:
        object_id = int(name_or_id)
        inst = session.query(model_type).get(object_id)
        if inst is None:
            raise ValueError("No instance of type %s with id %r" %
                             (model_type, name_or_id))
        return inst
    except ValueError:
        try:
            inst = session.query(model_type).filter(
                model_type.name == name_or_id).one()
            return inst
        except Exception:
            raise click.BadParameter("Could not locate an instance of %r with identifier %r" % (
                model_type.__name__, name_or_id))


def validate_database_unlocked(database_connection):
    try:
        db = DatabaseBoundOperation(database_connection)
        db.session.add(GlycanHypothesis(name="_____not_real_do_not_use______"))
        db.session.rollback()
        return True
    except OperationalError:
        return False


def validate_ms1_feature_name(feature_name):
    try:
        return ms1_model_features[feature_name]
    except KeyError:
        raise click.Abort(
            "Could not recognize scoring feature by name %r" % (
                feature_name,))


def strip_site_root(type, value, tb):
    msg = traceback.format_exception(type, value, tb)
    sanitized = []
    for i, line in enumerate(msg):
        if 'site-packages' in line:
            sanitized.append(line.split("site-packages")[1])
        else:
            sanitized.append(line)
    print(''.join(sanitized))


class RelativeMassErrorParam(click.types.FloatParamType):
    name = 'NUMBER'

    def convert(self, value, param, ctx):
        value = super(RelativeMassErrorParam, self).convert(value, param, ctx)
        if value >= 1:
            self.fail("mass error value must be less than 1, as "
                      "in parts-per-million error tolerance (e.g. 1e-5 for "
                      "10 parts-per-million error tolerance)")
        if value > 1e-3:
            click.secho(
                "Warning: %r has a relatively large margin, %f" % (
                    getattr(param, "human_readable_name", param),
                    value), fg='yellow')
        if value <= 0:
            self.fail("mass error value must be greater than 0.")
        return value


class DatabaseConnectionParam(click.types.StringParamType):
    name = "CONN"

    def __init__(self, exists=False):
        self.exists = exists

    def convert(self, value, param, ctx):
        value = super(DatabaseConnectionParam, self).convert(value, param, ctx)
        try:
            parse_engine_uri(value)
            return value
        except ArgumentError:
            # not a uri
            if self.exists:
                if not os.path.exists(value):
                    raise self.fail(
                        "Database file {} does not exist.".format(value), param, ctx)
            return value


class SmallSampleFDRCorrectionParam(click.ParamType):
    name = '0-1.0/on/off/auto'

    error_message_template = (
        "%s is not a valid option for FDR strategy selection. "
        "If numerical, it must be between 0 and 1.0, otherwise it must "
        "be one of auto, on, or off."
    )

    def convert(self, value, param, ctx):
        value = value.lower()
        try:
            threshold = float(value)
            if threshold > 1 or threshold < 0:
                self.fail(self.error_message_template % value, param, ctx)
        except ValueError:
            value = value.strip()
            if value == 'auto':
                threshold = 0.5
            elif value == 'off':
                threshold = 0.0
            elif value == 'on':
                threshold = 1.0
            else:
                self.fail(self.error_message_template % value, param, ctx)
            return threshold
