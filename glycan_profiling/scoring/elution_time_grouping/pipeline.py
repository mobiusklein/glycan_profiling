import os
from collections import defaultdict

import numpy as np

import glycopeptidepy

from glycan_profiling.task import TaskBase

from .structure import GlycopeptideChromatogramProxy
from .cross_run import ReplicatedAbundanceWeightedPeptideFactorElutionTimeFitter


class GlycopeptideElutionTimeModeler(TaskBase):
    _model_class = ReplicatedAbundanceWeightedPeptideFactorElutionTimeFitter

    def __init__(self, glycopeptide_chromatograms, factors=None, refit_filter=0.01, replicate_key_attr=None):
        if replicate_key_attr is None:
            replicate_key_attr = 'analysis_name'
        self.replicate_key_attr = replicate_key_attr
        if not isinstance(glycopeptide_chromatograms[0], GlycopeptideChromatogramProxy):
            glycopeptide_chromatograms = [
                GlycopeptideChromatogramProxy.from_obj(i) for i in glycopeptide_chromatograms]
        self.glycopeptide_chromatograms = glycopeptide_chromatograms
        self.factors = factors
        if self.factors is None:
            self.factors = self._infer_factors()
        self.joint_model = None
        self.refit_filter = refit_filter
        self.by_peptide = defaultdict(list)
        self.peptide_specific_models = dict()
        self.delta_by_factor = dict()
        self._partition_by_sequence()

    def _partition_by_sequence(self):
        for record in self.glycopeptide_chromatograms:
            key = glycopeptidepy.parse(str(record.structure)).deglycosylate()
            self.by_peptide[key].append(record)

    def _deltas_for(self, monosaccharide):
        deltas = []
        for _backbone, cases in self.by_peptide.items():
            for target in cases:
                gc = target.glycan_composition.clone()
                gc[monosaccharide] += 1
                key = self.joint_model._get_replicate_key(target)
                for case in cases:
                    if case.glycan_composition == gc and self.joint_model._get_replicate_key(case) == key:
                        deltas.append(case.apex_time - target.apex_time)
        return np.array(deltas)

    def _infer_factors(self):
        keys = set()
        for record in self.glycopeptide_chromatograms:
            keys.update(record.glycan_composition)
        keys = sorted(map(str, keys))
        return keys

    def fit_model(self, glycopeptide_chromatograms):
        model = self._model_class(
            glycopeptide_chromatograms, self.factors,
            replicate_key_attr=self.replicate_key_attr)
        model.fit()
        return model

    def fit(self):
        self.log("Fitting Joint Model")
        model = self.fit_model(self.glycopeptide_chromatograms)
        self.log("R^2: %0.3f" % (model.R2(), ))
        if self.refit_filter != 0.0:
            self.log("Filtering Training Data")
            filtered_cases = [
                case for case in self.glycopeptide_chromatograms
                if model.score(case) > self.refit_filter
            ]
            self.log("Re-fitting After Filtering")
            model = self.fit_model(filtered_cases)
            self.log("R^2: %0.3f" % (model.R2(), ))
        self.log('\n' + model.summary())
        self.joint_model = model
        factors = sorted(self.factors)
        self.log("Measuring Single Monosaccharide Deltas, Median and MAD")
        for key in factors:
            deltas = self._deltas_for(key)
            self.delta_by_factor[key] = deltas
            self.log("%s:   %0.3f   %0.3f" %
                     (key,
                      np.median(deltas),
                      np.median(np.abs(deltas - np.median(deltas))
                      )))
        for key, members in self.by_peptide.items():
            distinct_members = set(str(m.structure) for m in members)
            self.log("Fitting Model For %s (%d observations, %d distinct)" % (key, len(members), len(distinct_members)))
            if len(distinct_members) - 1 <= len(self.factors):
                self.log("Too few distinct observations for %s" % (key, ))
                continue
            model = self.fit_model(members)
            self.log("R^2: %0.3f" % (model.R2(), ))
            if self.refit_filter != 0.0:
                self.log("Filtering Training Data")
                filtered_cases = [
                    case for case in members
                    if model.score(case) > self.refit_filter
                ]
                self.log("Re-fitting After Filtering")
                model = self.fit_model(filtered_cases)
                self.log("R^2: %0.3f" % (model.R2(), ))
            self.log('\n' + model.summary())
            self.peptide_specific_models[key] = model
            joint_perf = np.mean(map(self.joint_model.score, members))
            spec_perf = np.mean(map(model.score, members))
            self.log("Mean Peptide Model Score: %0.3f" % (spec_perf, ))
            self.log("Mean Joint Model Score:   %0.3f" % (joint_perf, ))

    def evaluate(self):
        for key, group in self.by_peptide.items():
            self.log("Evaluating %s" % key)
            for obs in group:
                model = self._model_for(obs)
                score = model.score(obs)
                pred = model.predict(obs)
                delta = model._get_apex_time(obs) - pred
                obs.annotations['score'] = score
                obs.annotations['predicted_apex_time'] = pred
                obs.annotations['delta_apex_time'] = delta
                self.log("\t%s: %0.2f @ %0.2f (%s%0.2f)" % (
                    obs.structure, score, pred,
                    "+" if delta > 0 else '-', abs(delta)
                    ))

    def _model_for(self, observation):
        key = glycopeptidepy.parse(str(observation.structure)).deglycosylate()
        model = self.peptide_specific_models.get(key, self.joint_model)
        return model

    def predict(self, observation):
        model = self._model_for(observation)
        return model.predict(observation)

    def score(self, observation):
        model = self._model_for(observation)
        return model.score(observation)

    def write(self, path):
        from glycan_profiling.output.report.base import render_plot
        from glycan_profiling.plotting.base import figax
        if not os.path.exists(path):
            os.makedirs(path)
        elif not os.path.isdir(path):
            raise IOError("Expected a path to a directory, %s is a file!" % (path, ))
        pjoin = os.path.join
        with open(pjoin(path, "scored_chromatograms.csv"), 'wt') as fh:
            GlycopeptideChromatogramProxy.to_csv(self.glycopeptide_chromatograms, fh)
        with open(pjoin(path, "joint_model_parameters.csv"), 'wt') as fh:
            self.joint_model.to_csv(fh)
        with open(pjoin(path, "joint_model_predplot.png"), 'wb') as fh:
            ax = figax()
            self.joint_model.prediction_plot(ax=ax)
            fh.write(render_plot(ax).getvalue())

        for key, model in self.peptide_specific_models.items():
            with open(pjoin(path, "%s_model_parameters.csv" % (key, )), 'wt') as fh:
                model.to_csv(fh)
            with open(pjoin(path, "%s_model_predplot.png" % (key, )), 'wb') as fh:
                ax = figax()
                model.prediction_plot(ax=ax)
                fh.write(render_plot(ax).getvalue())