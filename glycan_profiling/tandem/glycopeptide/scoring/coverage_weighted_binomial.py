from ...spectrum_matcher_base import SpectrumMatcherBase

from .binomial_score import BinomialSpectrumMatcher, binomial_intensity, binomial_fragments_matched
from .simple_score import SimpleCoverageScorer, is_glycosylated
from .fragment_match_map import FragmentMatchMap


class CoverageWeightedBinomialScorer(BinomialSpectrumMatcher, SimpleCoverageScorer):
    def __init__(self, scan, sequence):
        BinomialSpectrumMatcher.__init__(self, scan, sequence)
        self.glycosylated_b_ion_count = 0
        self.glycosylated_y_ion_count = 0

    def match(self, error_tolerance=2e-5, *args, **kwargs):
        solution_map = FragmentMatchMap()
        spectrum = self.spectrum
        n_theoretical = 0

        for frag in self.target.glycan_fragments(
                all_series=False, allow_ambiguous=False,
                include_large_glycan_fragments=False,
                maximum_fragment_size=4):
            peak = spectrum.has_peak(frag.mass, error_tolerance)
            if peak:
                solution_map.add(peak, frag)
                try:
                    self._sanitized_spectrum.remove(peak)
                except KeyError:
                    continue

        n_glycosylated_b_ions = 0
        for frags in self.target.get_fragments('b'):
            glycosylated_position = False
            n_theoretical += 1
            for frag in frags:
                glycosylated_position |= is_glycosylated(frag)
                peak = spectrum.has_peak(frag.mass, error_tolerance)
                if peak:
                    solution_map.add(peak, frag)
            if glycosylated_position:
                n_glycosylated_b_ions += 1

        n_glycosylated_y_ions = 0
        for frags in self.target.get_fragments('y'):
            glycosylated_position = False
            n_theoretical += 1
            for frag in frags:
                glycosylated_position |= is_glycosylated(frag)
                peak = spectrum.has_peak(frag.mass, error_tolerance)
                if peak:
                    solution_map.add(peak, frag)
            if glycosylated_position:
                n_glycosylated_y_ions += 1

        for frag in self.target.stub_fragments(extended=True):
            # n_theoretical += 1
            peak = spectrum.has_peak(frag.mass, error_tolerance)
            if peak:
                solution_map.add(peak, frag)

        self.n_theoretical = n_theoretical
        self.glycosylated_b_ion_count = n_glycosylated_b_ions
        self.glycosylated_y_ion_count = n_glycosylated_y_ions
        self.solution_map = solution_map
        return solution_map

    def calculate_score(self, match_tolerance=2e-5, *args, **kwargs):
        bin_score = BinomialSpectrumMatcher.calculate_score(self, match_tolerance)
        coverage_score = SimpleCoverageScorer.calculate_score(self)
        self._score = bin_score * coverage_score
        return self._score