'''
proforma - Proteoform and Peptidoform Notation
==============================================

ProForma is a notation for defining modified amino acid sequences using
a set of controlled vocabularies, as well as encoding uncertain or partial
information about localization. See `ProForma specification <https://www.psidev.info/proforma>`_
for more up-to-date information.

Strictly speaking, this implementation supports ProForma v2.

Data Access
-----------

:py:func:`parse_proforma` - The primary interface for parsing ProForma strings.

    >>> parse_proforma("EM[Oxidation]EVT[#g1(0.01)]S[#g1(0.09)]ES[Phospho#g1(0.90)]PEK")
        ([('E', None),
          ('M', GenericModification('Oxidation', None, None)),
          ('E', None),
          ('V', None),
          ('T', LocalizationMarker(0.01, None, '#g1')),
          ('S', LocalizationMarker(0.09, None, '#g1')),
          ('E', None),
          ('S',
          GenericModification('Phospho', [LocalizationMarker(0.9, None, '#g1')], '#g1')),
          ('P', None),
          ('E', None),
          ('K', None)],
         {'n_term': None,
          'c_term': None,
          'unlocalized_modifications': [],
          'labile_modifications': [],
          'fixed_modifications': [],
          'intervals': [],
          'isotopes': [],
          'group_ids': ['#g1']})

:py:func:`to_proforma` - Format a sequence and set of properties as ProForma text.


Classes
-------

:py:class:`ProForma` - An object oriented version of the parsing and formatting code,
coupled with minimal information about mass and position data.

Dependencies
------------

To resolve PSI-MOD, XL-MOD, and GNO identifiers, :mod:`psims` is required.

'''

from pyteomics.mass.mass import calculate_mass
import re
import warnings
from collections import namedtuple, defaultdict, deque
from functools import partial

try:
    from enum import Enum
except ImportError:
    # Python 2 doesn't have a builtin Enum type
    Enum = object


from pyteomics import parser
from pyteomics.mass import Composition, std_aa_mass, Unimod
from pyteomics.auxiliary import PyteomicsError, BasicComposition
from pyteomics.auxiliary.utils import add_metaclass

# To eventually be implemented with pyteomics port?
try:
    from psims.controlled_vocabulary.controlled_vocabulary import (load_psimod, load_xlmod, load_gno, obo_cache)
except ImportError:
    def _needs_psims(name):
        raise ImportError("Loading %s requires the `psims` library. To access it, please install `psims`" % name)

    load_psimod = partial(_needs_psims, 'PSIMOD')
    load_xlmod = partial(_needs_psims, 'XLMOD')
    load_gno = partial(_needs_psims, 'GNO')
    obo_cache = None


std_aa_mass = std_aa_mass.copy()
std_aa_mass['X'] = 0


class ProFormaError(PyteomicsError):
    def __init__(self, message, index=None, parser_state=None, **kwargs):
        super(ProFormaError, self).__init__(PyteomicsError, message, index, parser_state)
        self.message = message
        self.index = index
        self.parser_state = parser_state


class PrefixSavingMeta(type):
    '''A subclass-registering-metaclass that provides easy
    lookup of subclasses by prefix attributes.
    '''

    def __new__(mcs, name, parents, attrs):
        new_type = type.__new__(mcs, name, parents, attrs)
        prefix = attrs.get("prefix_name")
        if prefix:
            new_type.prefix_map[prefix.lower()] = new_type
        short = attrs.get("short_prefix")
        if short:
            new_type.prefix_map[short.lower()] = new_type
        return new_type

    def find_by_tag(self, tag_name):
        if tag_name is None:
            raise ValueError("tag_name cannot be None!")
        tag_name = tag_name.lower()
        return self.prefix_map[tag_name]


class TagTypeEnum(Enum):
    unimod = 0
    psimod = 1
    massmod = 2
    generic = 3
    info = 4
    gnome = 5
    xlmod = 6

    formula = 7
    glycan = 8

    localization_marker = 9
    position_label = 10
    group_placeholder = 999


_sentinel = object()


@add_metaclass(PrefixSavingMeta)
class TagBase(object):
    '''A base class for all tag types.

    Attributes
    ----------
    type: Enum
        An element of :class:`TagTypeEnum` saying what kind of tag this is.
    value: object
        The data stored in this tag, usually an externally controlled name
    extra: list
        Any extra tags that were nested within this tag. Usually limited to INFO
        tags but may be other synonymous controlled vocabulary terms.
    group_id: str or None
        A short label denoting which group, if any, this tag belongs to
    '''
    __slots__ = ("type", "value", "extra", "group_id")

    prefix_name = None
    short_prefix = None
    prefix_map = {}

    def __init__(self, type, value, extra=None, group_id=None):
        self.type = type
        self.value = value
        self.extra = extra
        self.group_id = group_id

    def __str__(self):
        part = self._format_main()
        if self.extra:
            rest = [str(e) for e in self.extra]
            label = '|'.join([part] + rest)
        else:
            label = part
        if self.group_id:
            label = '%s%s' % (label, self.group_id)
        return '%s' % label

    def __repr__(self):
        template = "{self.__class__.__name__}({self.value!r}, {self.extra!r}, {self.group_id!r})"
        return template.format(self=self)

    def __eq__(self, other):
        if other is None:
            return False
        if isinstance(other, str):
            return str(self) == other
        return (self.type == other.type) and (self.value == other.value) and (self.extra == other.extra) \
            and (self.group_id == other.group_id)

    def __ne__(self, other):
        return not self == other

    def find_tag_type(self, tag_type):
        '''Search this tag or tag collection for elements with a particular
        tag type and return them.

        Parameters
        ----------
        tag_type : TagTypeEnum
            A label from :class:`TagTypeEnum`, or an equivalent type.

        Returns
        -------
        matches : list
            The list of all tags in this object which match the requested tag type.
        '''
        out = []
        if self.type == tag_type:
            out.append(self)
        if not self.extra:
            return out
        for e in self.extra:
            if e.type == tag_type:
                out.append(e)
        return out

    @classmethod
    def parse(cls, buffer):
        return process_tag_tokens(buffer)


class GroupLabelBase(TagBase):
    __slots__ = ()

    def __str__(self):
        part = self._format_main()
        if self.extra:
            rest = [str(e) for e in self.extra]
            label = '|'.join([part] + rest)
        else:
            label = part
        return '%s' % label


class PositionLabelTag(GroupLabelBase):
    '''A tag to mark that a position is involved in a group in some way, but does
    not imply any specific semantics.
    '''
    __slots__ = ()

    def __init__(self, value=None, extra=None, group_id=None):
        assert group_id is not None
        value = group_id
        super(PositionLabelTag, self).__init__(
            TagTypeEnum.position_label, value, extra, group_id)

    def _format_main(self):
        return "{self.group_id}".format(self=self)


class LocalizationMarker(GroupLabelBase):
    '''A tag to mark a particular localization site
    '''
    __slots__ = ()

    def __init__(self, value, extra=None, group_id=None):
        assert group_id is not None
        super(LocalizationMarker, self).__init__(
            TagTypeEnum.localization_marker, float(value), extra, group_id)

    def _format_main(self):
        return "{self.group_id}({self.value:.4g})".format(self=self)


class InformationTag(TagBase):
    '''A tag carrying free text describing the location
    '''
    __slots__ = ()

    prefix_name = "INFO"

    def __init__(self, value, extra=None, group_id=None):
        super(InformationTag, self).__init__(
            TagTypeEnum.info, str(value), extra, group_id)

    def _format_main(self):
        return str(self.value)


class MassModification(TagBase):
    '''A modification defined purely by a signed mass shift in Daltons.

    The value of a :class:`MassModification` is always a :class:`float`
    '''
    __slots__ = ('_significant_figures', )

    prefix_name = "Obs"

    def __init__(self, value, extra=None, group_id=None):
        if isinstance(value, str):
            sigfigs = len(value.split('.')[-1].rstrip('0'))
        else:
            sigfigs = 4
        self._significant_figures = sigfigs
        super(MassModification, self).__init__(
            TagTypeEnum.massmod, float(value), extra, group_id)

    def _format_main(self):
        if self.value >= 0:
            return ('+{0:0.{1}f}'.format(self.value, self._significant_figures)).rstrip('0').rstrip('.')
        else:
            return ('{0:0.{1}f}'.format(self.value, self._significant_figures)).rstrip('0').rstrip('.')

    @property
    def mass(self):
        return self.value


class ModificationResolver(object):
    def __init__(self, name, **kwargs):
        self.name = name
        self._database = None

    def load_database(self):
        raise NotImplementedError()

    @property
    def database(self):
        if not self._database:
            self._database = self.load_database()
        return self._database

    def resolve(self, name=None, id=None, **kwargs):
        raise NotImplementedError()

    def __call__(self, name=None, id=None, **kwargs):
        return self.resolve(name, id, **kwargs)


class UnimodResolver(ModificationResolver):
    def __init__(self, **kwargs):
        super(UnimodResolver, self).__init__("unimod", **kwargs)
        self._database = kwargs.get("database")
        self.strict = kwargs.get("strict", True)

    def load_database(self):
        return Unimod()

    def resolve(self, name=None, id=None, **kwargs):
        strict = kwargs.get("strict", self.strict)
        exhaustive = kwargs.get("exhaustive", True)
        if name is not None:
            defn = self.database.by_title(name, strict=strict)
            if not defn:
                defn = self.database.by_name(name, strict=strict)
            if not defn and exhaustive and strict:
                defn = self.database.by_title(name, strict=False)
                if not defn:
                    defn = self.database.by_name(name, strict=False)
            if defn and isinstance(defn, list):
                warnings.warn(
                    "Multiple matches found for {!r} in Unimod, taking the first, {}.".format(
                        name, defn[0]['record_id']))
                defn = defn[0]
            if not defn:
                raise KeyError(name)
        elif id is not None:
            defn = self.database.by_id(id)
            if not defn:
                raise KeyError(id)
        else:
            raise ValueError("Must provide one of `name` or `id`")
        return {
            'composition': defn['composition'],
            'name': defn['title'],
            'id': defn['record_id'],
            'mass': defn['mono_mass'],
            'provider': self.name
        }


class PSIModResolver(ModificationResolver):
    def __init__(self, **kwargs):
        super(PSIModResolver, self).__init__('psimod', **kwargs)
        self._database = kwargs.get("database")

    def load_database(self):
        return load_psimod()

    def resolve(self, name=None, id=None, **kwargs):
        if name is not None:
            defn = self.database[name]
        elif id is not None:
            defn = self.database['MOD:{:05d}'.format(id)]
        else:
            raise ValueError("Must provide one of `name` or `id`")
        mass = float(defn.DiffMono.strip()[1:-1])
        composition = Composition(defn.DiffFormula.strip()[1:-1].replace(" ", ''))
        return {
            'mass': mass,
            'composition': composition,
            'name': defn.name,
            'id': defn.id,
            'provider': self.name
        }


class XLMODResolver(ModificationResolver):
    def __init__(self, **kwargs):
        super(XLMODResolver, self).__init__('xlmod', **kwargs)
        self._database = kwargs.get("database")

    def load_database(self):
        return load_psimod()

    def resolve(self, name=None, id=None, **kwargs):
        if name is not None:
            defn = self.database[name]
        elif id is not None:
            defn = self.database['XLMOD:{:05d}'.format(id)]
        else:
            raise ValueError("Must provide one of `name` or `id`")
        mass = float(defn['monoIsotopicMass'])
        if 'deadEndFormula' in defn:
            composition = Composition(defn['deadEndFormula'].replace(" ", '').replace("D", "H[2]"))
        elif 'bridgeFormula' in defn:
            composition = Composition(
                defn['bridgeFormula'].replace(" ", '').replace("D", "H[2]"))
        return {
            'mass': mass,
            'composition': composition,
            'name': defn.name,
            'id': defn.id,
            'provider': self.name
        }

# TODO: Implement resolve walking up the graph to get the mass. Can't really
# get any more information without glypy/glyspace interaction
class GNOResolver(ModificationResolver):
    mass_pattern = re.compile(r"(\d+(:?\.\d+)) Da")

    def __init__(self, **kwargs):
        super(GNOResolver, self).__init__('gnome', **kwargs)
        self._database = kwargs.get("database")

    def load_database(self):
        return load_gno()

    def get_mass_from_term(self, term):
        root_id = 'GNO:00000001'
        parent = term.parent()
        if isinstance(parent, list):
            parent = parent[0]
        while parent.id != root_id:
            next_parent = term.parent()
            if isinstance(next_parent, list):
                next_parent = next_parent[0]
            if next_parent.id == root_id:
                break
            parent = next_parent
        match = self.mass_pattern.search(parent.name)
        if not match:
            return None
        return float(match.group(1))

    def resolve(self, name=None, id=None, **kwargs):
        if name is not None:
            term = self.database[name]
        elif id is not None:
            term = self.database[id]
        else:
            raise ValueError("Must provide one of `name` or `id`")
        rec = {
            "name":term.name,
            "id": term.id,
            "provider": self.name,
            "composition": None,
            "mass": self.get_mass_from_term(term)
        }


class GenericResolver(ModificationResolver):

    def __init__(self, resolvers, **kwargs):
        super(GenericResolver, self).__init__('generic', **kwargs)
        self.resolvers = list(resolvers)

    def load_database(self):
        return None

    def resolve(self, name=None, id=None, **kwargs):
        defn = None
        for resolver in self.resolvers:
            try:
                defn = resolver(name=name, id=id, **kwargs)
            except (KeyError):
                continue
        if defn is None:
            if name is None:
                raise KeyError(id)
            elif id is None:
                raise KeyError(name)
            else:
                raise ValueError("Must provide one of `name` or `id`")
        return defn


class ModificationBase(TagBase):
    '''A base class for all modification tags with marked prefixes.
    '''

    _tag_type = None
    __slots__ = ('_definition', )

    def __init__(self, value, extra=None, group_id=None):
        super(ModificationBase, self).__init__(
            self._tag_type, value, extra, group_id)
        self._definition = None

    @property
    def definition(self):
        if self._definition is None:
            self._definition = self.resolve()
        return self._definition

    @property
    def mass(self):
        return self.definition['mass']

    @property
    def composition(self):
        return self.definition.get('composition')

    @property
    def id(self):
        return self.definition.get('id')

    @property
    def name(self):
        return self.definition.get('name')

    @property
    def provider(self):
        return self.definition.get('provider')

    def _populate_from_definition(self, definition):
        self._definition = definition

    def _format_main(self):
        return "{self.prefix_name}:{self.value}".format(self=self)

    def _parse_identifier(self):
        tokens = self.value.split(":", 1)
        if len(tokens) > 1:
            value = tokens[1]
        else:
            value = self.value
        if value.isdigit():
            id = int(value)
            name = None
        else:
            name = value
            id = None
        return name, id

    def resolve(self):
        '''Find the term and return it's properties
        '''
        keys = self._parse_identifier()
        return self.resolver(*keys)


class FormulaModification(ModificationBase):
    prefix_name = "Formula"

    isotope_pattern = re.compile(r'\[(?P<isotope>\d+)(?P<element>[A-Z][a-z]*)(?P<quantity>[\-+]?\d+)\]')
    _tag_type = TagTypeEnum.formula

    def _normalize_isotope_notation(self, match):
        '''Rewrite ProForma isotope notation to Pyteomics-compatible
        isotope notation.

        Parameters
        ----------
        match : Match
            The matched isotope notation string parsed by the regular expression.

        Returns
        reformatted : str
            The re-written isotope notation
        '''
        parts = match.groupdict()
        return "{element}[{isotope}]{quantity}".format(**parts)

    def resolve(self):
        normalized = ''.join(self.value.split(" "))
        # If there is a [ character in the formula, we know there are isotopes which
        # need to be normalized.
        if '[' in normalized:
            normalized = self.isotope_pattern.sub(self._normalize_isotope_notation, normalized)
        composition = Composition(formula=normalized)
        return {
            "mass": composition.mass(),
            "composition": composition,
            "name": self.value
        }


class GlycanModification(ModificationBase):
    prefix_name = "Glycan"

    _tag_type = TagTypeEnum.glycan

    valid_monosaccharides = {
        "Hex": (162.0528, Composition("C6H10O5")),
        "HexNAc": (203.0793, Composition("C8H13N1O5")),
        "HexS": (242.009, Composition("C6H10O8S1")),
        "HexP": (242.0191, Composition("C6H11O8P1")),
        "HexNAcS": (283.0361, Composition("C8H13N1O8S1")),
        "dHex": (146.0579, Composition("C6H10O4")),
        "NeuAc": (291.0954, Composition("C11H17N1O8")),
        "NeuGc": (307.0903, Composition("C11H17N1O9")),
        "Pen": (132.0422, Composition("C5H8O4")),
        "Fuc": (146.0579, Composition("C6H10O4"))
    }

    tokenizer = re.compile(r"([A-Za-z]+)\s*(\d*)\s*")

    @property
    def monosaccharides(self):
        return self.definition.get('monosaccharides')

    def resolve(self):
        composite = BasicComposition()
        for tok, cnt in self.tokenizer.findall(self.value):
            if cnt:
                cnt = int(cnt)
            else:
                cnt = 1
            if tok not in self.valid_monosaccharides:
                raise ValueError("{tok!r} is not a valid monosaccharide name".format(**locals()))
            composite[tok] += cnt
        mass = 0
        chemcomp = Composition()
        for key, cnt in composite.items():
            m, c = self.valid_monosaccharides[key]
            mass += m * cnt
            chemcomp += c * cnt
        return {
            "mass": mass,
            "composition": chemcomp,
            "name": self.value,
            "monosaccharides": composite
        }


class UnimodModification(ModificationBase):
    __slots__ = ()

    resolver = UnimodResolver()

    prefix_name = "UNIMOD"
    short_prefix = "U"
    _tag_type = TagTypeEnum.unimod


class PSIModModification(ModificationBase):
    __slots__ = ()

    resolver = PSIModResolver()

    prefix_name = "MOD"
    short_prefix = 'M'
    _tag_type = TagTypeEnum.psimod


class GNOmeModification(ModificationBase):
    __slots__ = ()

    resolver = GNOResolver()

    prefix_name = "GNO"
    # short_prefix = 'G'
    _tag_type = TagTypeEnum.gnome


class XLMODModification(ModificationBase):
    __slots__ = ()

    resolver = XLMODResolver()

    prefix_name = "XLMOD"
    # short_prefix = 'XL'
    _tag_type = TagTypeEnum.xlmod


class GenericModification(ModificationBase):
    __slots__ = ()
    _tag_type = TagTypeEnum.generic
    resolver = GenericResolver([
        # Do exact matching here first. Then default to non-strict matching as a final
        # correction effort.
        partial(UnimodModification.resolver, exhaustive=False),
        PSIModModification.resolver,
        XLMODModification.resolver,
        GNOmeModification.resolver,
        # Some really common names aren't actually found in the XML exactly, so default
        # to non-strict matching now to avoid masking other sources here.
        partial(UnimodModification.resolver, strict=False)
    ])

    def __init__(self, value, extra=None, group_id=None):
        super(GenericModification, self).__init__(
            value, extra, group_id)

    def _format_main(self):
        return self.value

    def resolve(self):
        '''Find the term, searching through all available vocabularies and
        return the first match's properties
        '''
        keys = self._parse_identifier()
        defn = None
        try:
            defn = UnimodModification.resolver(*keys)
        except KeyError:
            pass
        if defn is not None:
            return defn
        raise KeyError(keys)


def split_tags(tokens):
    '''Split a token array into discrete sets of tag
    tokens.

    Parameters
    ----------
    tokens: list
        The characters of the tag token buffer

    Returns
    -------
    list of list:
        The tokens for each contained tag
    '''
    starts = [0]
    ends = []
    for i, c in enumerate(tokens):
        if c == '|':
            ends.append(i)
            starts.append(i + 1)
        elif (i != 0 and c == '#'):
            ends.append(i)
            starts.append(i)
    ends.append(len(tokens))
    out = []
    for i, start in enumerate(starts):
        end = ends[i]
        tag = tokens[start:end]
        if len(tag) == 0:
            continue
        # Short circuit on INFO tags which can't be broken
        # if (tag[0] == 'i' and tag[:5] == ['i', 'n', 'f', 'o', ':']) or (tag[0] == 'I' and tag[:5] == ['I', 'N', 'F', 'O', ':']):
        #     tag = tokens[start:]
        #     out.append(tag)
        #     break
        out.append(tag)
    return out


def find_prefix(tokens):
    '''Find the prefix, if any of the tag defined by `tokens`
    delimited by ":".

    Parameters
    ----------
    tokens: list
        The tag tokens to search

    Returns
    -------
    prefix: str or None
        The prefix string, if found
    rest: str
        The rest of the tokens, merged as a string
    '''
    for i, c in enumerate(tokens):
        if c == ':':
            return ''.join(tokens[:i]), ''.join(tokens[i + 1:])
    return None, ''.join(tokens)


def process_marker(tokens):
    '''Process a marker, which is a tag whose value starts with #.

    Parameters
    ----------
    tokens: list
        The tag tokens to parse

    Returns
    -------
    PositionLabelTag or LocalizationMarker
    '''
    if tokens[1:3] == 'XL':
        return PositionLabelTag(None, group_id=''.join(tokens))
    else:
        group_id = None
        value = None
        for i, c in enumerate(tokens):
            if c == '(':
                group_id = ''.join(tokens[:i])
                if tokens[-1] != ')':
                    raise Exception(
                        "Localization marker with score missing closing parenthesis")
                value = float(''.join(tokens[i + 1:-1]))
                return LocalizationMarker(value, group_id=group_id)
        else:
            group_id = ''.join(tokens)
            return PositionLabelTag(group_id=group_id)


def process_tag_tokens(tokens):
    '''Convert a tag token buffer into a parsed :class:`TagBase` instance
    of the appropriate sub-type with zero or more sub-tags.

    Parameters
    ----------
    tokens: list
        The tokens to parse

    Returns
    -------
    TagBase:
        The parsed tag
    '''
    parts = split_tags(tokens)
    main_tag = parts[0]
    if main_tag[0] in ('+', '-'):
        main_tag = ''.join(main_tag)
        main_tag = MassModification(main_tag)
    elif main_tag[0] == '#':
        main_tag = process_marker(main_tag)
    else:
        prefix, value = find_prefix(main_tag)
        if prefix is None:
            main_tag = GenericModification(''.join(value))
        else:
            tag_type = TagBase.find_by_tag(prefix)
            main_tag = tag_type(value)
    if len(parts) > 1:
        extras = []
        for part in parts[1:]:
            prefix, value = find_prefix(part)
            if prefix is None:
                if value[0] == "#":
                    marker = process_marker(value)
                    if isinstance(marker, PositionLabelTag):
                        main_tag.group_id = ''.join(value)
                    else:
                        main_tag.group_id = marker.group_id
                        extras.append(marker)
                else:
                    extras.append(GenericModification(''.join(value)))
            else:
                tag_type = TagBase.find_by_tag(prefix)
                extras.append(tag_type(value))
        main_tag.extra = extras
    return main_tag


class ModificationRule(object):
    '''Define a fixed modification rule which dictates a modification tag is
    always applied at one or more amino acid residues.

    Attributes
    ----------
    modification_tag: TagBase
        The modification to apply
    targets: list
        The list of amino acids this applies to
    '''
    __slots__ = ('modification_tag', 'targets')

    def __init__(self, modification_tag, targets=None):
        self.modification_tag = modification_tag
        self.targets = targets

    def __eq__(self, other):
        if other is None:
            return False
        return self.modification_tag == other.modification_tag and self.targets == other.targets

    def __ne__(self, other):
        return not self == other

    def __str__(self):
        targets = ','.join(self.targets)
        return "<[{self.modification_tag}]@{targets}>".format(self=self, targets=targets)

    def __repr__(self):
        return "{self.__class__.__name__}({self.modification_tag!r}, {self.targets})".format(self=self)


class StableIsotope(object):
    '''Define a fixed isotope that is applied globally to all amino acids.

    Attributes
    ----------
    isotope: str
        The stable isotope string, of the form [<isotope-number>]<element> or a special
        isotopoform's name.
    '''
    __slots__ = ('isotope', )

    def __init__(self, isotope):
        self.isotope = isotope

    def __eq__(self, other):
        if other is None:
            return False
        return self.isotope == other.isotope

    def __ne__(self, other):
        return not self == other

    def __str__(self):
        return "<{self.isotope}>".format(self=self)

    def __repr__(self):
        return "{self.__class__.__name__}({self.isotope})".format(self=self)


class TaggedInterval(object):
    '''Define a fixed interval over the associated sequence which contains the localization
    of the associated tag.

    Attributes
    ----------
    start: int
        The starting position (inclusive) of the interval along the primary sequence
    end: int
        The ending position (exclusive) of the interval along the primary sequence
    tag: TagBase
        The tag being localized
    '''
    __slots__ = ('start', 'end', 'tag')

    def __init__(self, start, end=None, tag=None):
        self.start = start
        self.end = end
        self.tag = tag

    def __eq__(self, other):
        if other is None:
            return False
        return self.start == other.start and self.end == other.end and self.tag == other.tag

    def __ne__(self, other):
        return not self == other

    def __str__(self):
        return "({self.start}-{self.end}){self.tag!r}".format(self=self)

    def __repr__(self):
        return "{self.__class__.__name__}({self.start}, {self.end}, {self.tag})".format(self=self)

    def as_slice(self):
        return slice(self.start, self.end)


class TokenBuffer(object):
    '''A token buffer that wraps the accumulation and reset logic
    of a list of :class:`str` objects.

    Implements a subset of the Sequence protocol.

    Attributes
    ----------
    buffer: list
        The list of tokens accumulated since the last parsing.
    '''
    def __init__(self, initial=None):
        self.buffer = list(initial or [])
        self.boundaries = []

    def append(self, c):
        '''Append a new character to the buffer.

        Parameters
        ----------
        c: str
            The character appended
        '''
        self.buffer.append(c)

    def reset(self):
        '''Discard the content of the current buffer.
        '''
        if self.buffer:
            self.buffer = []
        if self.boundaries:
            self.boundaries = []

    def __bool__(self):
        return bool(self.buffer)

    def __iter__(self):
        return iter(self.buffer)

    def __getitem__(self, i):
        return self.buffer[i]

    def __len__(self):
        return len(self.buffer)

    def tokenize(self):
        i = 0
        pieces = []
        for k in self.boundaries + [len(self)]:
            piece = self.buffer[i:k]
            i = k
            pieces.append(piece)
        return pieces

    def _transform(self, value):
        return value

    def process(self):
        if self.boundaries:
            value = [self._transform(v) for v in self.tokenize()]
        else:
            value = self._transform(self.buffer)
        self.reset()
        return value

    def bound(self):
        k = len(self)
        self.boundaries.append(k)
        return k

    def __call__(self):
        return self.process()


class NumberParser(TokenBuffer):
    '''A buffer which accumulates tokens until it is asked to parse them into
    :class:`int` instances.

    Implements a subset of the Sequence protocol.

    Attributes
    ----------
    buffer: list
        The list of tokens accumulated since the last parsing.
    '''

    def _transform(self, value):
        return int(''.join(value))


class TagParser(TokenBuffer):
    '''A buffer which accumulates tokens until it is asked to parse them into
    :class:`TagBase` instances.

    Implements a subset of the Sequence protocol.

    Attributes
    ----------
    buffer: list
        The list of tokens accumulated since the last parsing.
    group_ids: set
        The set of all group IDs that have been produced so far.
    '''

    def __init__(self, initial=None, group_ids=None):
        super(TagParser, self).__init__(initial)
        if group_ids:
            self.group_ids = set(group_ids)
        else:
            self.group_ids = set()

    def _transform(self, value):
        tag = process_tag_tokens(value)
        if tag.group_id:
            self.group_ids.add(tag.group_id)
        return tag

    def process(self):
        value = super(TagParser, self).process()
        if not isinstance(value, list):
            value = [value]
        return value


class ParserStateEnum(Enum):
    before_sequence = 0
    tag_before_sequence = 1
    global_tag = 2
    fixed_spec = 3
    labile_tag = 4
    sequence = 5
    tag_in_sequence = 6
    interval_tag = 7
    tag_after_sequence = 8
    stable_isotope = 9
    post_tag_before = 10
    unlocalized_count = 11
    post_global = 12
    post_global_aa = 13
    post_interval_tag = 14
    done = 999


BEFORE = ParserStateEnum.before_sequence
TAG_BEFORE = ParserStateEnum.tag_before_sequence
FIXED = ParserStateEnum.fixed_spec
GLOBAL = ParserStateEnum.global_tag
ISOTOPE = ParserStateEnum.stable_isotope
LABILE = ParserStateEnum.labile_tag
SEQ = ParserStateEnum.sequence
TAG = ParserStateEnum.tag_in_sequence
INTERVAL_TAG = ParserStateEnum.interval_tag
TAG_AFTER = ParserStateEnum.tag_after_sequence
POST_TAG_BEFORE = ParserStateEnum.post_tag_before
UNLOCALIZED_COUNT = ParserStateEnum.unlocalized_count
POST_GLOBAL = ParserStateEnum.post_global
POST_GLOBAL_AA = ParserStateEnum.post_global_aa
POST_INTERVAL_TAG = ParserStateEnum.post_interval_tag
DONE = ParserStateEnum.done

VALID_AA = set("QWERTYIPASDFGHKLCVNMXUOJZB")

def parse_proforma(sequence):
    '''Tokenize a ProForma sequence into a sequence of amino acid+tag positions, and a
    mapping of sequence-spanning modifiers.

    .. note::
        This is a state machine parser, but with certain sub-state paths
        unrolled to avoid an explosion of formal intermediary states.

    Parameters
    ----------
    sequence: str
        The sequence to parse

    Returns
    -------
    parsed_sequence: list[tuple[str, TagBase]]
        The (amino acid: str, TagBase or None) pairs denoting the positions along the primary sequence
    modifiers: dict
        A mapping listing the labile modifications, fixed modifications, stable isotopes, unlocalized
        modifications, tagged intervals, and group IDs
    '''
    labile_modifications = []
    fixed_modifications = []
    unlocalized_modifications = []
    intervals = []
    isotopes = []

    n_term = None
    c_term = None

    i = 0
    n = len(sequence)

    positions = []
    state = BEFORE
    depth = 0

    current_aa = None
    current_tag = TagParser()
    current_interval = None
    current_unlocalized_count = NumberParser()
    current_aa_targets = TokenBuffer()

    while i < n:
        c = sequence[i]
        i += 1
        if state == BEFORE:
            if c == '[':
                state = TAG_BEFORE
                depth = 1
            elif c == '{':
                state = LABILE
                depth = 1
            elif c == '<':
                state = FIXED
            elif c in VALID_AA:
                current_aa = c
                state = SEQ
            else:
                raise ProFormaError(
                    "Error In State {state}, unexpected {c} found at index {i}".format(**locals()), i, state)
        elif state == SEQ:
            if c in VALID_AA:
                positions.append((current_aa, current_tag() if current_tag else None))
                current_aa = c
            elif c == '[':
                state = TAG
                if current_tag:
                    current_tag.bound()
                depth = 1
            elif c == '(':
                if current_interval is not None:
                    raise ProFormaError(
                        ("Error In State {state}, nested range found at index {i}. "
                         "Nested ranges are not yet supported by ProForma.").format(
                            **locals()), i, state)
                current_interval = TaggedInterval(len(positions) + 1)
            elif c == ')':
                positions.append(
                    (current_aa, current_tag() if current_tag else None))
                current_aa = None
                if current_interval is None:
                    raise ProFormaError("Error In State {state}, unexpected {c} found at index {i}".format(**locals()), i, state)
                else:
                    current_interval.end = len(positions)
                    if i >= n or sequence[i] != '[':
                        raise ProFormaError("Missing Interval Tag", i, state)
                    i += 1
                    depth = 1
                    state = INTERVAL_TAG
            elif c == '-':
                state = TAG_AFTER
                if i >= n or sequence[i] != '[':
                    raise ProFormaError("Missing Closing Tag", i, state)
                i += 1
                depth = 1
            else:
                raise ProFormaError("Error In State {state}, unexpected {c} found at index {i}".format(**locals()), i, state)
        elif state == TAG or state == TAG_BEFORE or state == TAG_AFTER or state == GLOBAL:
            if c == '[':
                depth += 1
                current_tag.append(c)
            elif c == ']':
                depth -= 1
                if depth <= 0:
                    depth = 0
                    if state == TAG:
                        state = SEQ
                    elif state == TAG_BEFORE:
                        state = POST_TAG_BEFORE
                    elif state == TAG_AFTER:
                        c_term = current_tag()
                        state = DONE
                    elif state == GLOBAL:
                        state = POST_GLOBAL
                else:
                    current_tag.append(c)
            else:
                current_tag.append(c)
        elif state == FIXED:
            if c == '[':
                state = GLOBAL
            else:
                # Do validation here
                state = ISOTOPE
                current_tag.append(c)
        elif state == ISOTOPE:
            if c != '>':
                current_tag.append(c)
            else:
                # Not technically a tag, but exploits the current buffer
                isotopes.append(StableIsotope(''.join(current_tag)))
                current_tag.reset()
                state = BEFORE
        elif state == LABILE:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth <= 0:
                    depth = 0
                    labile_modifications.append(current_tag()[0])
                    state = BEFORE
            else:
                current_tag.append(c)
        elif state == INTERVAL_TAG:
            if c == '[':
                depth += 1
                current_tag.append(c)
            elif c == ']':
                depth -= 1
                if depth <= 0:
                    state = POST_INTERVAL_TAG
                    depth = 0
                else:
                    current_tag.append(c)
            else:
                current_tag.append(c)
        elif state == POST_INTERVAL_TAG:
            if c == '[':
                current_tag.bound()
                state = INTERVAL_TAG
            elif c in VALID_AA:
                current_aa = c
                current_interval.tag = current_tag()
                intervals.append(current_interval)
                current_interval = None
                state = SEQ
            elif c == '-':
                state = TAG_AFTER
                if i >= n or sequence[i] != '[':
                    raise ProFormaError("Missing Closing Tag", i, state)
                i += 1
                depth = 1
        elif state == POST_TAG_BEFORE:
            if c == '?':
                unlocalized_modifications.append(current_tag()[0])
                state = BEFORE
            elif c == '-':
                n_term = current_tag()
                state = BEFORE
            elif c == '^':
                state = UNLOCALIZED_COUNT
            else:
                raise ProFormaError(
                    "Error In State {state}, unexpected {c} found at index {i}".format(**locals()), i, state)
        elif state == UNLOCALIZED_COUNT:
            if c.isdigit():
                current_unlocalized_count.append(c)
            elif c == '[':
                state = TAG_BEFORE
                depth = 1
                tag = current_tag()[0]
                multiplicity = current_unlocalized_count()
                for i in range(multiplicity):
                    unlocalized_modifications.append(tag)
            elif c == '?':
                state = BEFORE
                tag = current_tag()[0]
                multiplicity = current_unlocalized_count()
                for i in range(multiplicity):
                    unlocalized_modifications.append(tag)
            else:
                raise ProFormaError(
                    "Error In State {state}, unexpected {c} found at index {i}".format(**locals()), i, state)
        elif state == POST_GLOBAL:
            if c == '@':
                state = POST_GLOBAL_AA
            else:
                raise ProFormaError(
                    ("Error In State {state}, fixed modification detected without "
                     "target amino acids found at index {i}").format(**locals()), i, state)
        elif state == POST_GLOBAL_AA:
            if c in VALID_AA:
                current_aa_targets.append(c)
            elif c == '>':
                fixed_modifications.append(
                    ModificationRule(current_tag()[0], current_aa_targets()))
                state = BEFORE
            else:
                raise ProFormaError(
                    ("Error In State {state}, unclosed fixed modification rule").format(**locals()), i, state)
        else:
            raise ProFormaError("Error In State {state}, unexpected {c} found at index {i}".format(**locals()), i, state)
    if current_aa:
        positions.append((current_aa, current_tag() if current_tag else None))
    if state in (ISOTOPE, TAG, TAG_AFTER, TAG_BEFORE, LABILE, ):
        raise ProFormaError("Error In State {state}, unclosed group reached end of string!".format(**locals()), i, state)
    return positions, {
        'n_term': n_term,
        'c_term': c_term,
        'unlocalized_modifications': unlocalized_modifications,
        'labile_modifications': labile_modifications,
        'fixed_modifications': fixed_modifications,
        'intervals': intervals,
        'isotopes': isotopes,
        'group_ids': sorted(current_tag.group_ids)
    }


def to_proforma(sequence, n_term=None, c_term=None, unlocalized_modifications=None,
                labile_modifications=None, fixed_modifications=None, intervals=None,
                isotopes=None, group_ids=None):
    '''Convert a sequence plus modifiers into formatted text following the
    ProForma specification.

    Parameters
    ----------
    sequence : list[tuple[str, TagBase]]
        The primary sequence of the peptidoform/proteoform to render
    n_term : Optional[TagBase]
        The N-terminal modification, if any.
    c_term : Optional[TagBase]
        The C-terminal modification, if any.
    unlocalized_modifications : Optional[list[TagBase]]
        Any modifications which aren't assigned to a specific location.
    labile_modifications : Optional[list[TagBase]]
        Any labile modifications
    fixed_modifications : Optional[list[ModificationRule]]
        Any fixed modifications
    intervals : Optional[list[TaggedInterval]]
        A list of modified intervals, if any
    isotopes : Optional[list[StableIsotope]]
        Any global stable isotope labels applied
    group_ids : Optional[list[str]]
        Any group identifiers. This parameter is currently not used.

    Returns
    -------
    str
    '''
    primary = deque()
    for aa, tags in sequence:
        if not tags:
            primary.append(str(aa))
        else:
            primary.append(str(aa) + ''.join(['[{0!s}]'.format(t) for t in tags]))
    if intervals:
        for iv in sorted(intervals, key=lambda x: x.start):
            primary[iv.start] = '(' + primary[iv.start]

            primary[iv.end - 1] = '{0!s})'.format(
                primary[iv.end - 1]) + ''.join('[{!s}]'.format(t) for t in iv.tag)
    if n_term:
        primary.appendleft(''.join("[{!s}]".format(t) for t in n_term) + '-')
    if c_term:
        primary.append('-' + ''.join("[{!s}]".format(t) for t in c_term))
    if labile_modifications:
        primary.extendleft(['{{{!s}}}'.format(m) for m in labile_modifications])
    if unlocalized_modifications:
        primary.appendleft("?")
        primary.extendleft(['[{!s}]'.format(m) for m in unlocalized_modifications])
    if isotopes:
        primary.extendleft(['{!s}'.format(m) for m in isotopes])
    if fixed_modifications:
        primary.extendleft(['{!s}'.format(m) for m in fixed_modifications])
    return ''.join(primary)


class ProForma(object):
    def __init__(self, sequence, properties):
        self.sequence = sequence
        self.properties = properties

    def __str__(self):
        return to_proforma(self.sequence, **self.properties)

    def __repr__(self):
        return "{self.__class__.__name__}({self.sequence}, {self.properties})".format(self=self)

    def __getitem__(self, i):
        if isinstance(i, slice):
            props = self.properties.copy()

            return self.__class__(self.sequence[i], self.properties)
        else:
            return self.sequence[i]

    def __eq__(self, other):
        if isinstance(other, str):
            return str(self) == other
        elif other is None:
            return False
        else:
            return self.sequence == other.sequence and self.properties == other.properties

    def __ne__(self, other):
        return not self == other

    @classmethod
    def parse(cls, string):
        return cls(*parse_proforma(string))

    @property
    def mass(self):
        mass = 0.0

        fixed_modifications = self.properties['fixed_modifications']
        fixed_rules = {}
        for rule in fixed_modifications:
            for aa in rule.targets:
                fixed_rules[aa] = rule.modification_tag.mass

        for position in self.sequence:
            aa = position[0]
            try:
                mass += std_aa_mass[aa]
            except KeyError:
                warnings.warn("%r does not have an exact mass" % (aa, ))
            if aa in fixed_rules:
                mass += fixed_rules[aa]
            tags = position[1]
            if tags:
                for tag in tags:
                    try:
                        mass += tag.mass
                    except (AttributeError, KeyError):
                        continue
        for mod in self.properties['labile_modifications']:
            mass += mod.mass
        for mod in self.properties['unlocalized_modifications']:
            mass += mod.mass
        if self.properties.get('n_term'):
            for mod in self.properties['n_term']:
                try:
                    mass += mod.mass
                except (AttributeError, KeyError):
                    continue
        mass += calculate_mass(formula="H")
        if self.properties.get('c_term'):
            for mod in self.properties['c_term']:
                try:
                    mass += mod.mass
                except (AttributeError, KeyError):
                    continue

        mass += calculate_mass(formula="OH")
        for iv in self.properties['intervals']:
            try:
                mass += iv.tag.mass
            except (AttributeError, KeyError):
                continue
        return mass

    def find_tags_by_id(self, tag_id, include_position=True):
        if not tag_id.startswith("#"):
            tag_id = "#" + tag_id
        if tag_id not in self.properties['group_ids']:
            return []
        matches = []
        for i, (_token, tags) in enumerate(self.sequence):
            if tags:
                for tag in tags:
                    if tag.group_id == tag_id:
                        if include_position:
                            matches.append((i, tag))
                        else:
                            matches.append(tag)
        for iv in self.properties['intervals']:
            if iv.tag.group_id == tag_id:
                matches.append((iv, iv.tag) if include_position else iv.tag)
        for ulmod in self.properties['unlocalized_modifications']:
            if ulmod.group_id == tag_id:
                matches.append(('unlocalized_modifications', ulmod)
                               if include_position else ulmod)
        for lamod in self.properties['labile_modifications']:
            if lamod.group_id == tag_id:
                matches.append(('labile_modifications', lamod)
                               if include_position else lamod)
        return matches
