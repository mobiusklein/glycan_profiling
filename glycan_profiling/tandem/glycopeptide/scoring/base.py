import math


from glycopeptidepy.structure.fragment import ChemicalShift, IonSeries
from glycopeptidepy.structure.fragmentation_strategy import EXDFragmentationStrategy, HCDFragmentationStrategy
from ..core_search import glycan_side_group_count
from ...spectrum_match import SpectrumMatcherBase, ModelTreeNode


class GlycopeptideSpectrumMatcherBase(SpectrumMatcherBase):

    _glycan_score = None
    _peptide_score = None
    _glycan_coverage = None

    def _theoretical_mass(self):
        return self.target.total_mass

    def _match_oxonium_ions(self, error_tolerance=2e-5, masked_peaks=None):
        '''Note: This method is masked by a Cython implementation
        '''
        if masked_peaks is None:
            masked_peaks = set()
        for frag in self.target.glycan_fragments(
                all_series=False, allow_ambiguous=False,
                include_large_glycan_fragments=False,
                maximum_fragment_size=4):
            peak = self.spectrum.has_peak(frag.mass, error_tolerance)
            if peak and peak.index.neutral_mass not in masked_peaks:
                self.solution_map.add(peak, frag)
                masked_peaks.add(peak.index.neutral_mass)
        return masked_peaks

    def _match_stub_glycopeptides(self, error_tolerance=2e-5, masked_peaks=None, chemical_shift=None, extended_glycan_search=False):
        if masked_peaks is None:
            masked_peaks = set()
        if not extended_glycan_search:
            fragments = self.target.stub_fragments(extended=True)
        else:
            fragments = self.target.stub_fragments(extended=True, extended_fucosylation=True)
        for frag in fragments:
            for peak in self.spectrum.all_peaks_for(frag.mass, error_tolerance):
                # should we be masking these? peptides which have amino acids which are
                # approximately the same mass as a monosaccharide unit at ther terminus
                # can produce cases where a stub ion and a backbone fragment match the
                # same peak.
                #
                masked_peaks.add(peak.index.neutral_mass)
                self.solution_map.add(peak, frag)
            if chemical_shift is not None:
                shifted_mass = frag.mass + self.mass_shift.tandem_mass
                for peak in self.spectrum.all_peaks_for(shifted_mass, error_tolerance):
                    masked_peaks.add(peak.index.neutral_mass)
                    shifted_frag = frag.clone()
                    shifted_frag.chemical_shift = chemical_shift
                    shifted_frag.name += "+ %s" % (self.mass_shift.name,)
                    self.solution_map.add(peak, shifted_frag)
        return masked_peaks

    def get_fragments(self, series, strategy=None, **kwargs):
        fragments = self.target.get_fragments(series, strategy=strategy)
        return fragments

    def _match_backbone_series(self, series, error_tolerance=2e-5, masked_peaks=None, strategy=None,
                               include_neutral_losses=False):
        if strategy is None:
            strategy = HCDFragmentationStrategy
        if masked_peaks is None:
            masked_peaks = set()
        for frags in self.get_fragments(series, strategy=strategy, include_neutral_losses=include_neutral_losses):
            for frag in frags:
                for peak in self.spectrum.all_peaks_for(frag.mass, error_tolerance):
                    if peak.index.neutral_mass in masked_peaks:
                        continue
                    self.solution_map.add(peak, frag)

    def match(self, error_tolerance=2e-5, *args, **kwargs):
        masked_peaks = set()
        include_neutral_losses = kwargs.get("include_neutral_losses", False)
        extended_glycan_search = kwargs.get("extended_glycan_search", False)
        if self.mass_shift.tandem_mass != 0:
            chemical_shift = ChemicalShift(
                self.mass_shift.name, self.mass_shift.tandem_composition)
        else:
            chemical_shift = None

        is_hcd = self.is_hcd()
        is_exd = self.is_exd()
        if not is_hcd and not is_exd:
            is_hcd = True
        # handle glycan fragments from collisional dissociation
        if is_hcd:
            self._match_oxonium_ions(error_tolerance, masked_peaks=masked_peaks)
            self._match_stub_glycopeptides(error_tolerance, masked_peaks=masked_peaks,
                                           chemical_shift=chemical_shift,
                                           extended_glycan_search=extended_glycan_search)

        # handle N-term
        if is_hcd and not is_exd:
            self._match_backbone_series(
                IonSeries.b, error_tolerance, masked_peaks, HCDFragmentationStrategy, include_neutral_losses)
        elif is_exd:
            self._match_backbone_series(
                IonSeries.b, error_tolerance, masked_peaks, EXDFragmentationStrategy,
                include_neutral_losses)
            self._match_backbone_series(
                IonSeries.c, error_tolerance, masked_peaks, EXDFragmentationStrategy,
                include_neutral_losses)

        # handle C-term
        if is_hcd and not is_exd:
            self._match_backbone_series(
                IonSeries.y, error_tolerance, masked_peaks, HCDFragmentationStrategy,
                include_neutral_losses)
        elif is_exd:
            self._match_backbone_series(
                IonSeries.y, error_tolerance, masked_peaks, EXDFragmentationStrategy,
                include_neutral_losses)
            self._match_backbone_series(
                IonSeries.z, error_tolerance, masked_peaks, EXDFragmentationStrategy,
                include_neutral_losses)

        return self

    def peptide_score(self, *args, **kwargs):
        if self._peptide_score is None:
            self._peptide_score = self.calculate_peptide_score(*args, **kwargs)
        return self._peptide_score

    def calculate_peptide_score(self, *args, **kwargs):
        return 0

    def glycan_score(self, *args, **kwargs):
        if self._glycan_score is None:
            self._glycan_score = self.calculate_glycan_score(*args, **kwargs)
        return self._glycan_score

    def _glycan_side_group_count(self, glycan_composition):
        return glycan_side_group_count(glycan_composition)

    def _calculate_glycan_coverage(self, core_weight=0.4, coverage_weight=0.5, fragile_fucose=True, extended_glycan_search=False, *args, **kwargs):
        seen = set()
        series = IonSeries.stub_glycopeptide
        if not extended_glycan_search:
            theoretical_set = list(self.target.stub_fragments(extended=True))
        else:
            theoretical_set = list(self.target.stub_fragments(extended=True, extended_fucosylation=True))
        core_fragments = set()
        for frag in theoretical_set:
            if not frag.is_extended:
                core_fragments.add(frag.name)

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
        glycan_composition = self.target.glycan_composition
        n = self._get_internal_size(glycan_composition)
        k = 2.0
        if not fragile_fucose:
            side_group_count = self._glycan_side_group_count(glycan_composition)
            if side_group_count > 0:
                k = 1.0
        d = max(n * math.log(n) / k, n)
        core_coverage = ((len(core_matches) * 1.0) /
                         len(core_fragments)) ** core_weight
        extended_coverage = min(
            float(len(core_matches) + len(extended_matches)) / d, 1.0) ** coverage_weight
        coverage = core_coverage * extended_coverage
        self._glycan_coverage = coverage
        return coverage

    def glycan_coverage(self, core_weight=0.4, coverage_weight=0.5, fragile_fucose=True, extended_glycan_search=False, * args, **kwargs):
        if self._glycan_coverage is not None:
            return self._glycan_coverage
        self._glycan_coverage = self._calculate_glycan_coverage(
            core_weight, coverage_weight, fragile_fucose=fragile_fucose,
            extended_glycan_search=extended_glycan_search, *args, **kwargs)
        return self._glycan_coverage

    def calculate_glycan_score(self, *args, **kwargs):
        return 0

    def get_auxiliary_data(self):
        data = super(GlycopeptideSpectrumMatcherBase, self).get_auxiliary_data()
        data['score'] = self.score
        data['glycan_score'] = self.glycan_score()
        data['peptide_score'] = self.peptide_score()
        data['glycan_coverage'] = self.glycan_coverage()
        data['n_peaks'] = len(self.spectrum)
        data['n_fragment_matches'] = len(self.solution_map)
        return data

try:
    from glycan_profiling._c.tandem.tandem_scoring_helpers import _match_oxonium_ions, _match_stub_glycopeptides
    GlycopeptideSpectrumMatcherBase._match_oxonium_ions = _match_oxonium_ions
    GlycopeptideSpectrumMatcherBase._match_stub_glycopeptides = _match_stub_glycopeptides
except ImportError:
    pass
