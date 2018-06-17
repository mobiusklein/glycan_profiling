from collections import defaultdict

try:
    from collections import Sequence
except ImportError:
    from collections.abc import Sequence

from glypy.structure.glycan_composition import FrozenMonosaccharideResidue

from glycan_profiling.chromatogram_tree import Unmodified

from .fragment_match_map import SpectrumGraph

hexnac = FrozenMonosaccharideResidue.from_iupac_lite("HexNAc")
hexose = FrozenMonosaccharideResidue.from_iupac_lite("Hex")
xylose = FrozenMonosaccharideResidue.from_iupac_lite("Xyl")
fucose = FrozenMonosaccharideResidue.from_iupac_lite("Fuc")
neuac = FrozenMonosaccharideResidue.from_iupac_lite("NeuAc")
neugc = FrozenMonosaccharideResidue.from_iupac_lite("NeuGc")


class MassWrapper(object):
    def __init__(self, obj):
        self.obj = obj
        try:
            # object's mass is a method
            self._mass = obj.mass()
        except TypeError:
            # object's mass is a plain attribute
            self._mass = obj.mass

    def __repr__(self):
        return "{self.__class__.__name__}({self.obj})".format(self=self)

    def __eq__(self, other):
        return self.obj == other

    def __hash__(self):
        return hash(self.obj)

    def mass(self, *a, **kw):
        return self._mass


default_components = (hexnac, hexose, xylose, fucose, neuac)


class Path(object):
    def __init__(self, edge_list):
        self.transitions = edge_list
        self.total_signal = self._total_signal()
        self.start_mass = self[0].start.neutral_mass
        self.end_mass = self[-1].end.neutral_mass
        self._peaks_used = None
        self._edges_used = None

    @property
    def peaks(self):
        if self._peaks_used is None:
            self._peaks_used = self._build_peaks_set()
        return self._peaks_used

    def _build_peaks_set(self):
        peaks = set()
        for edge in self:
            peaks.add(edge.end)
        peaks.add(self[0].start)
        return peaks

    def _build_edges_used(self):
        mapping = defaultdict(list)
        for edge in self:
            mapping[edge.start, edge.end].append(edge)
        return mapping

    def __contains__(self, edge):
        return self.has_edge(edge, True)

    def has_edge(self, edge, match_annotation=False):
        if self._edges_used is None:
            self._edges_used = self._build_edges_used()
        key = (edge.start, edge.end)
        if key in self._edges_used:
            edges = self._edges_used[key]
            if match_annotation:
                for e in edges:
                    if e == edge:
                        return True
                else:
                    return False
            else:
                for e in edges:
                    if e.key != edge.key:
                        continue
                    if e.start != edge.start:
                        continue
                    if e.end != edge.end:
                        continue
                    return True
                else:
                    return False

    def has_peak(self, peak):
        if self._peaks_used is None:
            self._peaks_used = self._build_peaks_set()
        return peak in self._peaks_used

    def has_peaks(self, peaks_set):
        if self._peaks_used is None:
            self._peaks_used = self._build_peaks_set()
        return peaks_set & self._peaks_used

    def __iter__(self):
        return iter(self.transitions)

    def __getitem__(self, i):
        return self.transitions[i]

    def __len__(self):
        return len(self.transitions)

    def _total_signal(self):
        total = 0
        for edge in self:
            total += edge.end.intensity
        total += self[0].start.intensity
        return total

    def __repr__(self):
        return "%s(%s, %0.4e, %f, %f)" % (
            self.__class__.__name__,
            '->'.join(str(e.annotation) for e in self),
            self.total_signal, self.start_mass, self.end_mass
        )


class PathSet(Sequence):
    def __init__(self, paths, ordered=False):
        self.paths = (sorted(paths, key=lambda x: x.start_mass)
                      if not ordered else paths)

    def __getitem__(self, i):
        return self.paths[i]

    def __len__(self):
        return len(self.paths)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, str(self.paths)[1:-1])

    def _repr_pretty_(self, p, cycle):
        with p.group(9, "%s([" % self.__class__.__name__, "])"):
            for i, path in enumerate(self):
                if i:
                    p.text(",")
                    p.breakable()
                p.pretty(path)

    def is_path_disjoint(self, path):
        for p in self:
            if p.has_peaks(path.peaks):
                return False
        return True


class PathFinder(object):
    def __init__(self, components=None, product_error_tolerance=1e-5):
        if components is None:
            components = default_components
        self.components = list(map(MassWrapper, components))
        self.product_error_tolerance = product_error_tolerance

    def _find_edges(self, scan, mass_shift=Unmodified):
        graph = SpectrumGraph()
        has_tandem_shift = abs(mass_shift.tandem_mass) > 0
        for peak in scan.deconvoluted_peak_set:
            for component in self.components:
                for other_peak in scan.deconvoluted_peak_set.all_peaks_for(
                        peak.neutral_mass + component.mass(), self.product_error_tolerance):
                    graph.add(peak, other_peak, component.obj)
                if has_tandem_shift:
                    for other_peak in scan.deconvoluted_peak_set.all_peaks_for(
                            peak.neutral_mass + component.mass() + mass_shift.tandem_mass,
                            self.product_error_tolerance):
                        graph.add(peak, other_peak, component.obj)
        return graph

    def _init_paths(self, graph, limit=200):
        paths = []
        min_start_mass = max(c.mass() for c in self.components) + 1
        for path in graph.longest_paths(limit=limit):
            path = Path(path)
            if path.start_mass < min_start_mass:
                continue
            paths.append(path)
        return paths

    def _aggregate_paths(self, paths):
        groups = defaultdict(list)
        for path in paths:
            label = tuple(p.annotation for p in path)
            groups[label].append(path)
        return groups

    def paths(self, scan, mass_shift=Unmodified, limit=200):
        graph = self._find_edges(scan, mass_shift)
        paths = self._init_paths(graph, limit)
        return paths