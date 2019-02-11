import math

from .base import (
    ChemicalShift, ModelTreeNode, EXDFragmentationStrategy,
    HCDFragmentationStrategy, IonSeries)
from .binomial_score import BinomialSpectrumMatcher
from .simple_score import SimpleCoverageScorer, SignatureAwareCoverageScorer
from .precursor_mass_accuracy import MassAccuracyMixin


class CoverageWeightedBinomialScorer(BinomialSpectrumMatcher, SignatureAwareCoverageScorer, MassAccuracyMixin):

    def __init__(self, scan, sequence, mass_shift=None):
        super(CoverageWeightedBinomialScorer, self).__init__(scan, sequence, mass_shift)

    def _match_backbone_series(self, series, error_tolerance=2e-5, masked_peaks=None, strategy=None,
                               track_ions=True, **kwargs):
        if strategy is None:
            strategy = HCDFragmentationStrategy
        previous_position_glycosylated = False
        for frags in self.target.get_fragments(series, strategy=strategy):
            glycosylated_position = previous_position_glycosylated
            if track_ions:
                self.n_theoretical += 1
            for frag in frags:
                if not glycosylated_position:
                    glycosylated_position |= frag.is_glycosylated
                for peak in self.spectrum.all_peaks_for(frag.mass, error_tolerance):
                    if peak.index.neutral_mass in masked_peaks:
                        continue
                    self.solution_map.add(peak, frag)
            if glycosylated_position and track_ions:
                if series.direction > 0:
                    self.glycosylated_n_term_ion_count += 1
                else:
                    self.glycosylated_c_term_ion_count += 1
            previous_position_glycosylated = glycosylated_position

    def match(self, error_tolerance=2e-5, *args, **kwargs):
        return SignatureAwareCoverageScorer.match(self, error_tolerance=error_tolerance, *args, **kwargs)

    def calculate_score(self, error_tolerance=2e-5, backbone_weight=None, glycosylated_weight=None,
                        stub_weight=None, *args, **kwargs):
        bin_score = BinomialSpectrumMatcher.calculate_score(
            self, error_tolerance=error_tolerance)
        coverage_score = SimpleCoverageScorer.calculate_score(
            self, backbone_weight, glycosylated_weight, stub_weight)
        mass_accuracy = self._precursor_mass_accuracy_score()
        signature_component = self._signature_ion_score()
        self._score = bin_score * coverage_score + mass_accuracy + signature_component
        return self._score


class ShortPeptideCoverageWeightedBinomialScorer(CoverageWeightedBinomialScorer):
    stub_weight = 0.65


def _short_peptide_test(scan, target, *args, **kwargs):
    return len(target) < 10


CoverageWeightedBinomialModelTree = ModelTreeNode(CoverageWeightedBinomialScorer, {
    _short_peptide_test: ModelTreeNode(ShortPeptideCoverageWeightedBinomialScorer, {}),
})
