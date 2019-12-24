import re
from collections import OrderedDict

from sqlalchemy.ext.baked import bakery
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import (
    relationship, backref, Query, validates,
    deferred, object_session)
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy import (
    Column, Numeric, Integer, String, ForeignKey, PickleType,
    Boolean, Table, Text, Index)
from sqlalchemy.ext.mutable import MutableDict, MutableList

from glycan_profiling.serialize.base import (
    Base)


from .hypothesis import GlycopeptideHypothesis
from .glycan import GlycanCombination
from .generic import JSONType

from glycopeptidepy.structure import sequence, residue, PeptideSequenceBase
from glycan_profiling.structure.structure_loader import PeptideProteinRelation, FragmentCachingGlycopeptide
from glycan_profiling.structure.utils import LRUDict


class AminoAcidSequenceWrapperBase(PeptideSequenceBase):
    _sequence_obj = None

    def _get_sequence_str(self):
        raise NotImplementedError()

    def _get_sequence(self):
        if self._sequence_obj is None:
            self._sequence_obj = sequence.PeptideSequence(
                self._get_sequence_str())
        return self._sequence_obj

    def __iter__(self):
        return iter(self._get_sequence())

    def __getitem__(self, i):
        return self._get_sequence()[i]

    def __len__(self):
        return len(self._get_sequence())

    def __str__(self):
        return str(self._get_sequence())


class Protein(Base, AminoAcidSequenceWrapperBase):
    __tablename__ = "Protein"

    id = Column(Integer, primary_key=True, autoincrement=True)
    protein_sequence = Column(Text, default=u"")
    name = Column(String(128), index=True)
    other = Column(MutableDict.as_mutable(PickleType))
    hypothesis_id = Column(Integer, ForeignKey(
        GlycopeptideHypothesis.id, ondelete="CASCADE"))
    hypothesis = relationship(GlycopeptideHypothesis, backref=backref('proteins', lazy='dynamic'))

    def _get_sequence_str(self):
        return self.protein_sequence

    _n_glycan_sequon_sites = None

    @property
    def n_glycan_sequon_sites(self):
        if self._n_glycan_sequon_sites is None:
            sites = self.sites.filter(ProteinSite.name == ProteinSite.N_GLYCOSYLATION).all()
            if sites:
                self._n_glycan_sequon_sites = [int(i) for i in sites]
            elif self.sites.count() == 0:
                try:
                    self._n_glycan_sequon_sites = sequence.find_n_glycosylation_sequons(self._get_sequence())
                except residue.UnknownAminoAcidException:
                    return []
            else:
                return []
        return self._n_glycan_sequon_sites

    _o_glycan_sequon_sites = None

    @property
    def o_glycan_sequon_sites(self):
        if self._o_glycan_sequon_sites is None:
            sites = self.sites.filter(ProteinSite.name == ProteinSite.O_GLYCOSYLATION).all()
            if sites:
                self._o_glycan_sequon_sites = [int(i) for i in sites]
            elif self.sites.count() == 0:
                try:
                    self._o_glycan_sequon_sites = sequence.find_o_glycosylation_sequons(self._get_sequence())
                except residue.UnknownAminoAcidException:
                    return []
            else:
                return []
        return self._o_glycan_sequon_sites

    _glycosaminoglycan_sequon_sites = None

    @property
    def glycosaminoglycan_sequon_sites(self):
        if self._glycosaminoglycan_sequon_sites is None:
            sites = self.sites.filter(ProteinSite.name == ProteinSite.GAGYLATION).all()
            if sites:
                self._glycosaminoglycan_sequon_sites = [int(i) for i in sites]
            elif self.sites.count() == 0:
                try:
                    self._glycosaminoglycan_sequon_sites = sequence.find_glycosaminoglycan_sequons(
                        self._get_sequence())
                except residue.UnknownAminoAcidException:
                    return []
            else:
                return []
        return self._glycosaminoglycan_sequon_sites

    @property
    def glycosylation_sites(self):
        try:
            return self.n_glycan_sequon_sites  # + self.o_glycan_sequon_sites
        except residue.UnknownAminoAcidException:
            return []

    def _init_sites(self):
        try:
            n_glycosites = sequence.find_n_glycosylation_sequons(self._get_sequence())
            for n_glycosite in n_glycosites:
                self.sites.append(
                    ProteinSite(name=ProteinSite.N_GLYCOSYLATION, location=n_glycosite))
        except residue.UnknownAminoAcidException:
            pass

        try:
            o_glycosites = sequence.find_o_glycosylation_sequons(self._get_sequence())
            for o_glycosite in o_glycosites:
                self.sites.append(
                    ProteinSite(name=ProteinSite.O_GLYCOSYLATION, location=o_glycosite))
        except residue.UnknownAminoAcidException:
            pass

        try:
            gag_sites = sequence.find_glycosaminoglycan_sequons(self._get_sequence())
            for gag_site in gag_sites:
                self.sites.append(
                    ProteinSite(name=ProteinSite.GAGYLATION, location=gag_site))
        except residue.UnknownAminoAcidException:
            pass

    def __repr__(self):
        return "DBProtein({0}, {1}, {2}, {3}...)".format(
            self.id, self.name, self.glycosylation_sites,
            self.protein_sequence[:20] if self.protein_sequence is not None else "")

    def to_json(self, full=False):
        d = OrderedDict((
            ('id', self.id),
            ('name', self.name),
            ("glycosylation_sites", list(self.glycosylation_sites)),
            ('other', self.other)
        ))
        if full:
            d.update({
                "protein_sequence": self.protein_sequence
            })
            for k, v in self.__dict__.items():
                if isinstance(v, Query):
                    d[k + '_count'] = v.count()
        return d

    def reverse(self, copy_id=False, prefix=None, suffix=None):
        n = len(self.protein_sequence)
        sites = []
        for site in self.sites:  # pylint: disable=access-member-before-definition
            sites.append(site.__class__(name=site.name, location=n - site.location - 1))
        name = self.name
        if name.startswith(">"):
            if prefix:
                name = ">" + prefix + name[1:]
        if suffix:
            name = name + suffix

        inst = self.__class__(name=name, protein_sequence=self.protein_sequence[::-1])
        if copy_id:
            inst.id = self.id
        inst.sites = sites
        return inst


class ProteinSite(Base):
    __tablename__ = "ProteinSite"

    id = Column(Integer, primary_key=True)
    name = Column(String(32), index=True)
    location = Column(Integer, index=True)
    protein_id = Column(Integer, ForeignKey(Protein.id, ondelete="CASCADE"), index=True)
    protein = relationship(Protein, backref=backref("sites", lazy='dynamic'))

    N_GLYCOSYLATION = "N-Glycosylation"
    O_GLYCOSYLATION = "O-Glycosylation"
    GAGYLATION = "Glycosaminoglycosylation"

    def __repr__(self):
        return ("{self.__class__.__name__}(location={self.location}, "
                "name={self.name})").format(self=self)

    def __hash__(self):
        return hash((self.name, self.location))

    def __index__(self):
        return self.location

    def __int__(self):
        return self.location

    def __add__(self, other):
        return int(self) + int(other)

    def __radd__(self, other):
        return int(self) + int(other)

    def __eq__(self, other):
        if isinstance(other, ProteinSite):
            return self.location == other.location and self.name == other.name
        return int(self) == int(other)

    def __ne__(self, other):
        return not (self == other)


def _convert_class_name_to_collection_name(name):
    parts = re.split(r"([A-Z]+[a-z]+)", name)
    parts = [p.lower() for p in parts if p]
    return '_'.join(parts) + 's'


class PeptideBase(AminoAcidSequenceWrapperBase):
    @declared_attr
    def protein_id(self):
        return Column(Integer, ForeignKey(
            Protein.id, ondelete="CASCADE"), index=True)

    @declared_attr
    def hypothesis_id(self):
        return Column(Integer, ForeignKey(
            GlycopeptideHypothesis.id, ondelete="CASCADE"), index=True)

    @declared_attr
    def protein(self):
        if not hasattr(self, "__collection_name__"):
            name = _convert_class_name_to_collection_name(self.__name__)
        else:
            name = self.__collection_name__
        return relationship(Protein, backref=backref(name, lazy='dynamic'))

    calculated_mass = Column(Numeric(12, 6, asdecimal=False), index=True)
    formula = Column(String(128))

    def __iter__(self):
        return iter(self.convert())

    def __len__(self):
        return len(self.convert())

    @property
    def total_mass(self):
        return self.convert().total_mass

    _protein_relation = None

    @property
    def protein_relation(self):
        if self._protein_relation is None:
            peptide = self
            self._protein_relation = PeptideProteinRelation(
                peptide.start_position, peptide.end_position, peptide.protein_id,
                peptide.hypothesis_id)
        return self._protein_relation

    @property
    def start_position(self):
        return self.protein_relation.start_position

    @property
    def end_position(self):
        return self.protein_relation.end_position

    def overlaps(self, other):
        return self.protein_relation.overlaps(other.protein_relation)

    def spans(self, position):
        return position in self.protein_relation


class Peptide(PeptideBase, Base):
    __tablename__ = 'Peptide'

    id = Column(Integer, primary_key=True)

    count_glycosylation_sites = Column(Integer)
    count_missed_cleavages = Column(Integer)
    count_variable_modifications = Column(Integer)

    start_position = Column(Integer)
    end_position = Column(Integer)

    peptide_score = Column(Numeric(12, 6, asdecimal=False))
    _scores = deferred(Column("scores", JSONType))
    peptide_score_type = Column(String(56))

    base_peptide_sequence = Column(String(512))
    modified_peptide_sequence = Column(String(512))

    sequence_length = Column(Integer)

    peptide_modifications = Column(String(128))
    n_glycosylation_sites = Column(MutableList.as_mutable(PickleType))
    o_glycosylation_sites = Column(MutableList.as_mutable(PickleType))
    gagylation_sites = Column(MutableList.as_mutable(PickleType))

    hypothesis = relationship(GlycopeptideHypothesis, backref=backref('peptides', lazy='dynamic'))

    def _get_sequence_str(self):
        return self.modified_peptide_sequence

    def convert(self):
        inst = sequence.parse(self.modified_peptide_sequence)
        inst.id = self.id
        return inst

    def __repr__(self):
        return ("DBPeptideSequence({self.modified_peptide_sequence}, {self.n_glycosylation_sites},"
                " {self.start_position}, {self.end_position})").format(self=self)

    __table_args__ = (
        Index("ix_Peptide_mass_search_index", "hypothesis_id", "calculated_mass"),
        Index("ix_Peptide_coordinate_index", "id", "calculated_mass",
              "start_position", "end_position"),)

    @hybrid_method
    def spans(self, position):
        return position in self.protein_relation

    @spans.expression
    def spans(self, position):
        return (self.start_position <= position) & (position <= self.end_position)

    @hybrid_property
    def scores(self): # pylint: disable=method-hidden
        try:
            if self._scores is None:
                self.scores = []
            return self._scores
        except Exception:
            return []

    @scores.setter
    def scores(self, value):
        self._scores = value


class Glycopeptide(PeptideBase, Base):
    __tablename__ = "Glycopeptide"

    id = Column(Integer, primary_key=True)
    peptide_id = Column(Integer, ForeignKey(Peptide.id, ondelete='CASCADE'), index=True)
    glycan_combination_id = Column(Integer, ForeignKey(GlycanCombination.id, ondelete='CASCADE'), index=True)

    peptide = relationship(Peptide, backref=backref("glycopeptides", lazy='dynamic'))
    glycan_combination = relationship(GlycanCombination)

    glycopeptide_sequence = Column(String(1024))

    hypothesis = relationship(GlycopeptideHypothesis, backref=backref('glycopeptides', lazy='dynamic'))

    def _get_sequence_str(self):
        return self.glycopeptide_sequence

    def convert(self, peptide_relation_cache=None):
        if peptide_relation_cache is None:
            session = object_session(self)
            peptide_relation_cache = session.info.get("peptide_relation_cache")
            if peptide_relation_cache is None:
                peptide_relation_cache = session.info['peptide_relation_cache'] = LRUDict(maxsize=1024)
        inst = FragmentCachingGlycopeptide(self.glycopeptide_sequence)
        inst.id = self.id
        try:
            inst.protein_relation = peptide_relation_cache[self.peptide_id]
        except KeyError:
            session = object_session(self)
            peptide_props = session.query(
                Peptide.start_position, Peptide.end_position,
                Peptide.protein_id, Peptide.hypothesis_id).filter(Peptide.id == self.peptide_id).first()
            peptide_relation_cache[self.peptide_id] = inst.protein_relation = PeptideProteinRelation(*peptide_props)
        return inst

    def __repr__(self):
        return "DBGlycopeptideSequence({self.glycopeptide_sequence}, {self.calculated_mass})".format(self=self)
    _protein_relation = None

    @property
    def protein_relation(self):
        if self._protein_relation is None:
            peptide = self.peptide
            self._protein_relation = PeptideProteinRelation(
                peptide.start_position, peptide.end_position, peptide.protein_id,
                peptide.hypothesis_id)
        return self._protein_relation

    @property
    def glycan_composition(self):
        return self.glycan_combination.convert()

    __table_args__ = (
        Index("ix_Glycopeptide_mass_search_index", "hypothesis_id", "calculated_mass"),
        # Index("ix_Glycopeptide_mass_search_index_full", "calculated_mass", "hypothesis_id",
        #                                                 "peptide_id", "glycan_combination_id"),
    )
