import re
import warnings

from collections import namedtuple, defaultdict
try:
    from collections.abc import Mapping, Sequence
except ImportError:
    from collections import Mapping, Sequence

import numpy as np

from glypy.structure.glycan_composition import HashableGlycanComposition
from glycopeptidepy.structure.parser import strip_modifications

from glycan_profiling import serialize
from glycan_profiling.task import TaskBase
from glycan_profiling.structure import PeptideProteinRelation

from glycan_profiling.database import GlycanCompositionDiskBackedStructureDatabase

from glycan_profiling.database.builder.glycopeptide.proteomics.fasta import DeflineSuffix
from glycan_profiling.database.builder.glycopeptide.proteomics.sequence_tree import SuffixTree

from glycan_profiling.tandem.glycopeptide.identified_structure import IdentifiedGlycoprotein

from glycan_profiling.database.composition_network import NeighborhoodWalker, make_n_glycan_neighborhoods
from glycan_profiling.composition_distribution_model import (
    smooth_network, display_table, VariableObservationAggregation,
    GlycanCompositionSolutionRecord,
    AbundanceWeightedObservationAggregation)
from glycan_profiling.models import GeneralScorer, get_feature


GlycanPriorRecord = namedtuple("GlycanPriorRecord", ("score", "matched"))
_default_chromatogram_scorer = GeneralScorer.clone()
_default_chromatogram_scorer.add_feature(get_feature("null_charge"))


MINIMUM = 1e-4


from glycan_profiling.composition_distribution_model.site_model import *
MINIMUM = 1e-4


class GlycosylationSiteModel(object):

    def __init__(self, protein_name, position, site_distribution, lmbda, glycan_map):
        self.protein_name = protein_name
        self.position = position
        self.site_distribution = site_distribution
        self.lmbda = lmbda
        self.glycan_map = glycan_map

    def __getitem__(self, key):
        return self.glycan_map[key][0]

    def get_record(self, key):
        try:
            return self.glycan_map[key]
        except KeyError:
            return GlycanPriorRecord(MINIMUM, False)

    def to_dict(self):
        d = {}
        d['protein_name'] = self.protein_name
        d['position'] = self.position
        d['lmbda'] = self.lmbda
        d['site_distribution'] = dict(**self.site_distribution)
        d['glycan_map'] = {
            str(k): (v.score, v.matched) for k, v in self.glycan_map.items()
        }
        return d

    @classmethod
    def from_dict(cls, d):
        name = d['protein_name']
        position = d['position']
        lmbda = d['lmbda']
        try:
            site_distribution = d['site_distribution']
        except KeyError:
            site_distribution = d['tau']
        glycan_map = d['glycan_map']
        glycan_map = {
            HashableGlycanComposition.parse(k): GlycanPriorRecord(v[0], v[1])
            for k, v in glycan_map.items()
        }
        inst = cls(name, position, site_distribution, lmbda, glycan_map)
        return inst

    def _pack(self):
        new_map = {}
        for key, value in self.glycan_map.items():
            if value.score > MINIMUM:
                new_map[key] = value
        self.glycan_map = new_map

    def __repr__(self):
        template = ('{self.__class__.__name__}({self.protein_name!r}, {self.position}, '
                    '{site_distribution}, {self.lmbda}, <{glycan_map_size} Glycans>)')
        glycan_map_size = len(self.glycan_map)
        site_distribution = {k: v for k, v in self.site_distribution.items() if v > 0.0}
        return template.format(self=self, glycan_map_size=glycan_map_size, site_distribution=site_distribution)

    def copy(self, deep=False):
        dup = self.__class__(
            self.protein_name, self.position, self.site_distribution, self.lmbda, self.glycan_map)
        if deep:
            dup.site_distribution = dup.site_distribution.copy()
            dup.glycan_map = dup.glycan_map.copy()
        return dup

    def clone(self, *args, **kwargs):
        return self.copy(*args, **kwargs)


class GlycoproteinSiteSpecificGlycomeModel(object):
    def __init__(self, protein, glycosylation_sites=None):
        self.protein = protein
        self._glycosylation_sites = []
        self.glycosylation_sites = glycosylation_sites

    @property
    def glycosylation_sites(self):
        return self._glycosylation_sites

    @glycosylation_sites.setter
    def glycosylation_sites(self, glycosylation_sites):
        self._glycosylation_sites = sorted(glycosylation_sites or [], key=lambda x: x.position)

    def __getitem__(self, i):
        return self.glycosylation_sites[i]

    def __len__(self):
        return len(self.glycosylation_sites)

    @property
    def id(self):
        return self.protein.id

    @property
    def name(self):
        return self.protein.name

    def find_sites_in(self, start, end):
        spans = []
        for site in self.glycosylation_sites:
            if start <= site.position <= end:
                spans.append(site)
            elif end < site.position:
                break
        return spans

    def _guess_sites_from_sequence(self, sequence):
        prot_seq = str(self.protein)
        query_seq = strip_modifications(sequence)
        try:
            start = prot_seq.index(query_seq)
            end = start + len(query_seq)
            return PeptideProteinRelation(start, end, self.protein.id, self.protein.hypothesis_id)
        except ValueError:
            return None

    def score(self, glycopeptide):
        pr = glycopeptide.protein_relation
        sites = self.find_sites_in(pr.start_position, pr.end_position)
        if len(sites) > 1:
            raise NotImplementedError("Not compatible with multiple spanning glycosites (yet)")
        try:
            site = sites[0]
            try:
                rec = site.glycan_map[glycopeptide.glycan_composition]
            except KeyError:
                return MINIMUM
            return rec.score
        except IndexError:
            return MINIMUM

    @classmethod
    def bind_to_hypothesis(cls, session, site_models, hypothesis_id=1, fuzzy=True):
        by_protein_name = defaultdict(list)
        for site in site_models:
            by_protein_name[site.protein_name].append(site)
        protein_models = {}
        proteins = session.query(serialize.Protein).filter(
            serialize.Protein.hypothesis_id == hypothesis_id).all()
        protein_name_map = {prot.name: prot for prot in proteins}
        if fuzzy:
            tree = SuffixTree()
            for prot in proteins:
                tree.add_ngram(DeflineSuffix(prot.name, prot.name))

        for protein_name, sites in by_protein_name.items():
            if fuzzy:
                labels = list(tree.subsequences_of(protein_name))
                protein = protein_name_map[labels[0].original]
            else:
                protein = protein_name_map[protein_name]

            model = cls(protein, sites)
            protein_models[model.id] = model
        return protein_models

    def __repr__(self):
        template = "{self.__class__.__name__}({self.name}, {self.glycosylation_sites})"
        return template.format(self=self)


class ReversedProteinSiteReflectionGlycoproteinSiteSpecificGlycomeModel(GlycoproteinSiteSpecificGlycomeModel):
    @property
    def glycosylation_sites(self):
        return self._glycosylation_sites

    @glycosylation_sites.setter
    def glycosylation_sites(self, glycosylation_sites):
        temp = []
        n = len(str(self.protein))
        for site in glycosylation_sites:
            site = site.copy()
            site.position = n - site.position - 1
            temp.append(site)
        self._glycosylation_sites = sorted(temp or [], key=lambda x: x.position)


class GlycoproteomeModel(object):
    def __init__(self, glycoprotein_models):
        if isinstance(glycoprotein_models, Mapping):
            self.glycoprotein_models = dict(glycoprotein_models)
        else:
            self.glycoprotein_models = {
                ggm.id: ggm for ggm in glycoprotein_models
            }

    def find_model(self, glycopeptide):
        if glycopeptide.protein_relation is None:
            return None
        protein_id = glycopeptide.protein_relation.protein_id
        glycoprotein_model = self.glycoprotein_models[protein_id]
        return glycoprotein_model

    def score(self, spectrum_match):
        glycopeptide = spectrum_match.target
        glycoprotein_model = self.find_model(glycopeptide)
        if glycoprotein_model is None:
            score = MINIMUM
        else:
            score = glycoprotein_model.score(glycopeptide)
        return max(min(spectrum_match.score, score), 0)

    @classmethod
    def bind_to_hypothesis(cls, session, site_models, hypothesis_id=1, fuzzy=True):
        inst = cls(
            GlycoproteinSiteSpecificGlycomeModel.bind_to_hypothesis(
                session, site_models, hypothesis_id, fuzzy))
        return inst


class SubstringGlycoproteomeModel(object):
    def __init__(self, models):
        self.models = models
        self.sequence_to_model = {
            str(model.protein): model for model in models.values()
        }

    def get_models(self, glycopeptide):
        out = []
        seq = strip_modifications(glycopeptide)
        pattern = re.compile(seq)
        for case in self.sequence_to_model:
            if seq in case:
                bounds = pattern.finditer(case)
                for match in bounds:
                    protein_model = self.sequence_to_model[case]
                    site_models = protein_model.find_sites_in(match.start(), match.end())
                    out.append(site_models)
        return out

    def find_proteins(self, glycopeptide):
        out = []
        seq = strip_modifications(glycopeptide)
        pattern = re.compile(seq)
        for case in self.sequence_to_model:
            if seq in case:
                out.append(self.sequence_to_model[case])
        return out

    def score(self, glycopeptide):
        models = self.get_models(glycopeptide)
        if len(models) == 0:
            return MINIMUM
        if len(models) > 1:
            warnings.warn("Multiple proteins for {}".format(glycopeptide))
        sites = models[0]
        if len(sites) == 0:
            return MINIMUM
        if len(sites) > 1:
            warnings.warn("Multiple sites for {}".format(glycopeptide))
        try:
            acc = []
            for site in sites:
                try:
                    rec = site.glycan_map[glycopeptide.glycan_composition]
                    acc.append(rec.score)
                except KeyError:
                    pass
            return max(sum(acc) / len(acc), MINIMUM) if acc else MINIMUM
        except IndexError:
            return MINIMUM

    def __call__(self, glycopeptide):
        return self.get_models(glycopeptide)


class GlycosylationSiteModelBuilder(TaskBase):

    def __init__(self, glycan_graph, chromatogram_scorer=None, belongingness_matrix=None,
                 unobserved_penalty_scale=None, lambda_limit=0.2,
                 require_multiple_observations=True,
                 observation_aggregator=None):
        if observation_aggregator is None:
            observation_aggregator = VariableObservationAggregation
        if chromatogram_scorer is None:
            chromatogram_scorer = _default_chromatogram_scorer
        if unobserved_penalty_scale is None:
            unobserved_penalty_scale = 1.0
        self.network = glycan_graph
        if not self.network.neighborhoods:
            self.network.neighborhoods = make_n_glycan_neighborhoods()
        self.chromatogram_scorer = chromatogram_scorer
        self.belongingness_matrix = belongingness_matrix
        self.observation_aggregator = observation_aggregator
        self.require_multiple_observations = require_multiple_observations
        self.unobserved_penalty_scale = unobserved_penalty_scale
        self.lambda_limit = lambda_limit
        if self.belongingness_matrix is None:
            self.belongingness_matrix = self.build_belongingness_matrix()
        self.site_models = []

    def build_belongingness_matrix(self):
        network = self.network
        neighborhood_walker = NeighborhoodWalker(
            network, network.neighborhoods)
        belongingness_matrix = neighborhood_walker.build_belongingness_matrix()
        return belongingness_matrix

    def add_glycoprotein(self, glycoprotein, evaluate_chromatograms=False):
        self.log("Building Model for \"%s\"" % (glycoprotein.name, ))
        for i, site in enumerate(glycoprotein.site_map['N-Linked'].sites):
            gps_for_site = glycoprotein.site_map[
                'N-Linked'][glycoprotein.site_map['N-Linked'].sites[i]]
            gps_for_site = [
                gp for gp in gps_for_site if gp.chromatogram is not None]

            self.log('... %d Identified Glycopeptides At Site %d' %
                     (len(gps_for_site), site))

            glycopeptides = [
                gp for gp in gps_for_site if gp.chromatogram is not None]
            records = []
            for gp in glycopeptides:
                if evaluate_chromatograms:
                    ms1_score = self.chromatogram_scorer.logitscore(gp.chromatogram)
                else:
                    ms1_score = gp.ms1_score
                records.append(GlycanCompositionSolutionRecord(
                    gp.glycan_composition, ms1_score, gp.total_signal))

            self.fit_site_model(records)

        def _get_learnable_cases(self, observations):
            learnable_cases = [rec for rec in observations if rec.score > 0]

            if self.require_multiple_observations:
                agg = VariableObservationAggregation(self.network)
                agg.collect(learnable_cases)
                recs, var = agg.build_records()
                stable_cases = set([gc[0].glycan_composition for gc in filter(
                    lambda x: x[1] != 1.0, zip(recs, np.diag(var)))])
                self.log("... %d Stable Glycan Compositions" %
                         (len(stable_cases)))
                if len(stable_cases) == 0:
                    stable_cases = set([gc.glycan_composition for gc in recs])
                    self.log("... No Stable Cases Found. Using %d Glycan Compositions" % (
                        len(stable_cases), ))
                if len(stable_cases) == 0:
                    return []
            else:
                stable_cases = {
                    case.glycan_composition for case in learnable_cases}
            learnable_cases = [
                rec for rec in learnable_cases
                if rec.score > 0 and rec.glycan_composition in stable_cases
            ]
            return learnable_cases

        def fit_site_model(self, observations, site, protein):
            learnable_cases = self._get_learnable_cases(observations)

            if not learnable_cases:
                return None

            fitted_network, search_result, params = smooth_network(
                self.network, learnable_cases,
                belongingness_matrix=self.belongingness_matrix,
                observation_aggregator=VariableObservationAggregation)
            self.log("Lambda: %f" % (params.lmbda,))
            display_table([x.name for x in self.network.neighborhoods],
                          np.array(params.tau).reshape((-1, 1)))
            updated_params = params.clone()
            updated_params.lmbda = min(self.lambda_limit, params.lmbda)
            fitted_network = search_result.annotate_network(updated_params)
            for node in fitted_network:
                if node.marked:
                    node.score *= self.unobserved_penalty_scale

            site_distribution = dict(zip([x.name for x in self.network.neighborhoods],
                     updated_params.tau.tolist()))
            glycan_map = {
                str(node.glycan_composition): GlycanPriorRecord(node.score, not node.marked)
                for node in fitted_network
            }
            site_model = GlycosylationSiteModel(
                protein.name,
                site,
                site_distribution,
                updated_params.lmbda,
                glycan_map)
            self.site_models.append(site_model)
            return site_model
