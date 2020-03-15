import warnings
from collections import defaultdict, namedtuple

from glycan_profiling.task import TaskBase
from glycan_profiling.chromatogram_tree.utils import ArithmeticMapping
from glycan_profiling.chromatogram_tree.chromatogram import GlycopeptideChromatogram
from glycan_profiling.scoring import ChromatogramSolution
from glycan_profiling.tandem.chromatogram_mapping import TandemAnnotatedChromatogram
from glycan_profiling.tandem.identified_structure import IdentifiedStructure

from glycan_profiling.plotting import chromatogram_artist


MergeAction = namedtuple("MergeAction", ("label", "existing", "new", "shift"))
CreateAction = namedtuple(
    "CreateAction", ("label", "structure", "chromatogram", "shift"))


class SharedIdentification(object):
    def __init__(self, identification_key, identifications=None):
        if identifications is None:
            identifications = dict()
        self.identification_key = identification_key
        self.identifications = dict(identifications)

    @property
    def structure(self):
        return self.identification_key

    def __getitem__(self, key):
        return self.identifications[key]

    def __setitem__(self, key, value):
        self.identifications[key] = value

    def __contains__(self, key):
        return key in self.identifications

    def __iter__(self):
        return iter(self.identifications)

    def __len__(self):
        return len(self.identifications)

    def keys(self):
        return self.identifications.keys()

    def values(self):
        return self.identifications.values()

    def items(self):
        return self.identifications.items()

    def __repr__(self):
        template = "{self.__class__.__name__}({self.identification_key}, {self.identifications})"
        return template.format(self=self)

    def weighted_neutral_masses(self):
        result = ArithmeticMapping()
        for key, value in self.identifications.items():
            result[key] = value.weighted_neutral_mass
        return result

    def apex_times(self):
        result = ArithmeticMapping()
        for key, value in self.identifications.items():
            result[key] = value.apex_time
        return result

    def total_signals(self):
        result = ArithmeticMapping()
        for key, value in self.identifications.items():
            result[key] = value.total_signal
        return result

    def plot(self, ax=None, **kwargs):
        label_map = {v.chromatogram: k for k, v in self.items()}

        def labeler(chromatogram, *args, **kwargs):
            return label_map[chromatogram]

        art = chromatogram_artist.SmoothingChromatogramArtist(
            self.values(), label_peaks=False, ax=ax, **kwargs)
        return art.draw(label_function=labeler, legend_cols=1)


class MatchBetweenRunBuilder(TaskBase):
    def __init__(self, datasets):
        self.datasets = datasets
        self.feature_table = dict()
        self.build_feature_table()
        self.labels = sorted([mbd.label for mbd in self.datasets])

    def build_feature_table(self, mass_error_tolerance=1e-5, time_error_tolerance=2.0):
        for mbd in self.datasets:
            for ids in mbd.identified_structures:
                try:
                    shared = self.feature_table[ids.structure]
                except KeyError:
                    shared = SharedIdentification(ids.structure)
                    self.feature_table[ids.structure] = shared
                if mbd.label in shared:
                    current = shared[mbd.label]
                    self.log("Multiple entries for %r in %r" %
                             (ids, mbd.label))
                    shared[mbd.label] = max(
                        [current, ids], key=lambda x: x.total_signal or 0)
                    self.log("Chose %r" % (shared[mbd.label], ))
                else:
                    shared[mbd.label] = ids

    def get_by_label(self, label):
        for mbd in self.datasets:
            if mbd.label == label:
                return mbd
        return None

    def create(self, label, structure, chromatogram, shift):
        self.log("Creating %r as %r in %r" % (chromatogram, structure, label))
        mbd = self.get_by_label(label)
        ids = mbd.create(structure, chromatogram, shift)
        if ids is not None:
            shared_id = self.feature_table[structure]
            shared_id[mbd.label] = ids
            mbd.add(ids)

    def merge(self, label, structure, new, shift):
        self.log("Merging %r into %r in %r" % (new, structure, label))
        mbd = self.get_by_label(label)
        if isinstance(new, IdentifiedStructure):
            if new == structure:
                raise ValueError("Cannot merge the same structure")
            elif new.structure.full_structure_equality(structure.structure):
                raise ValueError("Cannot merge equivalent structure")
            else:
                raise ValueError("Cannot merge ambiguous structure")
            new = new.chromatogram
        mbd.merge(structure, new, shift)

    def search(self, shared_id, mass_error_tolerance=1e-5, time_error_tolerance=2.0):
        create_actions = set()
        merge_actions = set()

        for inst in shared_id.values():
            merges, creates = self.find(
                inst, mass_error_tolerance, time_error_tolerance)
            create_actions.update(creates)
            merge_actions.update(merges)
        return merge_actions, create_actions

    def match_structure_between(self, shared_id, mass_error_tolerance=1e-5, time_error_tolerance=2.0):
        merge_actions, create_actions = self.search(
            shared_id, mass_error_tolerance, time_error_tolerance)

        create_actions = sorted(
            create_actions, key=lambda x: x.chromatogram.total_signal, reverse=True)
        merge_actions = sorted(
            merge_actions, key=lambda x: x.new.total_signal, reverse=True)

        for action in create_actions:
            self.create(action.label, action.structure,
                        action.chromatogram, action.shift)

        for action in merge_actions:
            self.merge(action.label, action.existing, action.new, action.shift)
        return merge_actions, create_actions

    def find(self, ids, mass_error_tolerance=1e-5, time_error_tolerance=2.0):
        create_actions = set()
        merge_actions = set()

        for mbd in self.datasets:
            shared_id = self.feature_table[ids.structure]
            out = mbd.find(ids, mass_error_tolerance, time_error_tolerance)
            # We've identified this structure in this sample already
            if mbd.label in shared_id:
                existing_match = shared_id[mbd.label]
                for entity, shift in out:
                    if isinstance(entity, IdentifiedStructure):
                        if entity == existing_match:
                            continue
                        if entity.structure != existing_match.structure:
                            if entity.structure.full_structure_equality(existing_match.structure):
                                # Then is the protein different? We'll probably deal with them
                                # soon.
                                self.log("Skipping Shared Structure %r in %r\n" % (
                                    existing_match.structure, mbd.label))
                            else:
                                # Totally different structure, emit a warning?
                                warnings.warn("Ambiguous 1 Link Between %r and %r with shift %r\n" % (
                                    existing_match.structure, entity.structure, shift))
                        else:
                            if existing_match.chromatogram and entity.chromatogram:
                                self.log("Multiple chromatograms for %r at %0.2f and %0.2f\n" % (
                                    existing_match.structure, existing_match.apex_time, entity.apex_time))
                    else:
                        # It's a chromatogram, is it a different mass shift state?
                        existing_apex_time = existing_match.apex_time
                        if existing_apex_time is None:
                            existing_apex_time = existing_match.tandem_solutions[0].scan_time
                        if abs(entity.apex_time - existing_apex_time) < time_error_tolerance:
                            if existing_match.chromatogram is not None and existing_match.chromatogram.common_nodes(entity):
                                self.log("Repeated attempt to merge %r and %r in %r\n" % (
                                    existing_match.structure, entity, mbd.label))
                                continue
                            merge_actions.add(MergeAction(
                                mbd.label, existing_match.structure, entity, shift))

            else:
                for entity, shift in out:
                    if isinstance(entity, IdentifiedStructure):
                        if entity.structure.full_structure_equality(ids.structure):
                            # Then is the protein different? Should already be handled above.
                            import pdb
                            pdb.set_trace()
                            warnings.warn("Ambiguous 2 Link Between %r and %r with shift %r in %r\n" % (
                                ids.structure, entity.structure, shift.name, mbd.label))
                        else:
                            # Totally different structure, emit a warning?
                            self.log("Ambiguous 3 Link Between %r and %r with shift %r in %r\n" % (
                                ids.structure, entity.structure, shift.name, mbd.label))
                    else:
                        # It's a chromatogram. Wrap it in something and add it to the shared_id
                        create_actions.add(CreateAction(
                            mbd.label, ids.structure, entity, shift))

        return merge_actions, create_actions