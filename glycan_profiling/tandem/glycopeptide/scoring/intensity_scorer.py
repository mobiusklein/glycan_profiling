import numpy as np
import math

from glycopeptidepy.structure.fragment import IonSeries

from .base import ModelTreeNode
from .precursor_mass_accuracy import MassAccuracyMixin
from .simple_score import SignatureAwareCoverageScorer


class LogIntensityScorer(SignatureAwareCoverageScorer, MassAccuracyMixin):

    def __init__(self, scan, sequence, mass_shift=None, *args, **kwargs):
        super(LogIntensityScorer, self).__init__(scan, sequence, mass_shift, *args, **kwargs)

    def _intensity_score(self, error_tolerance=2e-5, *args, **kwargs):
        total = 0
        seen = set()
        for peak, fragment in self.solution_map:
            if peak.index.neutral_mass in seen:
                continue
            seen.add(peak.index.neutral_mass)
            total += np.log10(peak.intensity)
        return total

    def calculate_score(self, error_tolerance=2e-5, peptide_weight=0.7, *args, **kwargs):
        glycan_weight = 1 - peptide_weight
        combo_score = self.peptide_score(error_tolerance
            ) * peptide_weight + self.glycan_score(error_tolerance) * glycan_weight
        mass_accuracy = self._precursor_mass_accuracy_score()
        signature_component = self._signature_ion_score(error_tolerance)
        self._score = combo_score + mass_accuracy + signature_component
        return self._score

    def peptide_score(self, error_tolerance=2e-5, coverage_weight=1.0):
        total = 0
        series_set = (IonSeries.b, IonSeries.y)
        seen = set()
        for peak_pair in self.solution_map:
            peak = peak_pair.peak
            if peak_pair.fragment.series in series_set and peak.index.neutral_mass not in seen:
                seen.add(peak.index.neutral_mass)
                total += np.log10(peak.intensity) * (1 - (abs(peak_pair.mass_accuracy()) / error_tolerance) ** 4)
        n_term, c_term = self._compute_coverage_vectors()[:2]
        coverage_score = ((n_term + c_term[::-1])).sum() / float((2 * len(self.target) - 1))
        score = total * coverage_score ** coverage_weight
        if np.isnan(score):
            return 0
        return score

    def glycan_score(self, error_tolerance=2e-5, core_weight=0.4, coverage_weight=0.6, *args, **kwargs):
        seen = set()
        series = IonSeries.stub_glycopeptide
        theoretical_set = list(self.target.stub_fragments(extended=True))
        core_fragments = set()
        for frag in theoretical_set:
            if not frag.is_extended:
                core_fragments.add(frag.name)

        total = 0
        core_matches = set()
        extended_matches = set()

        for peak_pair in self.solution_map:
            if peak_pair.fragment.series != series:
                continue
            elif peak_pair.fragment_name in core_fragments:
                core_matches.add(peak_pair.fragment_name)
            else:
                extended_matches.add(peak_pair.fragment_name)
            peak = peak_pair.peak
            if peak.index.neutral_mass not in seen:
                seen.add(peak.index.neutral_mass)
                total += np.log10(peak.intensity) * (1 - (abs(peak_pair.mass_accuracy()) / error_tolerance) ** 4)
        n = self._get_internal_size(self.target.glycan_composition)
        k = 2.0
        core_coverage = (len(core_matches) * 1.0) / len(core_fragments) ** core_weight
        extended_coverage = min(float(len(core_matches) + len(extended_matches)
            ) / (n * np.log(n) / k), 1.0) ** coverage_weight
        score = total * core_coverage * extended_coverage + 0.5 * self._signature_ion_score(error_tolerance)
        if np.isnan(score):
            return 0
        return score


class ShortPeptideLogIntensityScorer(LogIntensityScorer):
    stub_weight = 0.65


def _short_peptide_test(scan, target, *args, **kwargs):
    return len(target) < 10


LogIntensityModelTree = ModelTreeNode(LogIntensityScorer, {
    _short_peptide_test: ModelTreeNode(ShortPeptideLogIntensityScorer, {}),
})


class HyperscoreScorer(SignatureAwareCoverageScorer, MassAccuracyMixin):

    def _calculate_hyperscore(self, *args, **kwargs):
        n_term_intensity = 0
        c_term_intensity = 0
        stub_intensity = 0
        n_term = 0
        c_term = 0
        stub_count = 0
        for peak, fragment in self.solution_map:
            if fragment.series == "oxonium_ion":
                continue
            elif fragment.series == IonSeries.stub_glycopeptide:
                stub_count += 1
                stub_intensity += peak.intensity
            elif fragment.series in (IonSeries.b, IonSeries.c):
                n_term += 1
                n_term_intensity += peak.intensity
            elif fragment.series in (IonSeries.y, IonSeries.z):
                c_term += 1
                c_term_intensity += peak.intensity
        hyper = 0
        factors = [math.factorial(n_term), math.factorial(c_term), math.factorial(stub_count),
                   stub_intensity, n_term_intensity, c_term_intensity]
        for f in factors:
            hyper += math.log(f)

        return hyper

    def calculate_score(self, error_tolerance=2e-5, *args, **kwargs):
        hyperscore = self._calculate_hyperscore(error_tolerance, *args, **kwargs)
        mass_accuracy = self._precursor_mass_accuracy_score()
        signature_component = self._signature_ion_score(error_tolerance)
        self._score = hyperscore + mass_accuracy + signature_component

        return self._score
