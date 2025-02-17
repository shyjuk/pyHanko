import enum
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set, Type

from pyhanko.pdf_utils import generic, misc
from pyhanko.pdf_utils.crypt.cred_ser import SerialisableCredential


class PdfKeyNotAvailableError(misc.PdfReadError):
    pass


class AuthStatus(misc.OrderedEnum):
    """
    Describes the status after an authentication attempt.
    """

    FAILED = 0
    USER = 1
    OWNER = 2


@dataclass(frozen=True)
class AuthResult:
    """
    Describes the result of an authentication attempt.
    """

    status: AuthStatus
    """
    Authentication status after the authentication attempt.
    """

    permission_flags: Optional[int] = None
    """
    Granular permission flags. The precise meaning depends on the security
    handler.
    """


@enum.unique
class SecurityHandlerVersion(misc.VersionEnum):
    """
    Indicates the security handler's version.

    The enum constants are named more or less in accordance with the
    cryptographic algorithms they permit.
    """
    RC4_40 = 1
    RC4_LONGER_KEYS = 2
    RC4_OR_AES128 = 4
    AES256 = 5

    OTHER = None
    """
    Placeholder value for custom security handlers.
    """

    def as_pdf_object(self) -> generic.PdfObject:
        val = self.value
        return generic.NullObject() if val is None \
            else generic.NumberObject(val)

    @classmethod
    def from_number(cls, value) -> 'SecurityHandlerVersion':
        try:
            return SecurityHandlerVersion(value)
        except ValueError:
            return SecurityHandlerVersion.OTHER

    def check_key_length(self, key_length: int) -> int:
        if self == SecurityHandlerVersion.RC4_40:
            return 5
        elif self == SecurityHandlerVersion.AES256:
            return 32
        elif not (5 <= key_length <= 16) \
                and self <= SecurityHandlerVersion.RC4_OR_AES128:
            raise misc.PdfError("Key length must be between 5 and 16")
        return key_length


class SecurityHandler:
    """
    Generic PDF security handler interface.

    This class contains relatively little actual functionality, except for
    some common initialisation logic and bookkeeping machinery to register
    security handler implementations.

    :param version:
        Indicates the version of the security handler to use, as described
        in the specification. See :class:`.SecurityHandlerVersion`.
    :param legacy_keylen:
        Key length in bytes (only relevant for legacy encryption handlers).
    :param crypt_filter_config:
        The crypt filter configuration for the security handler, in the
        form of a :class:`.CryptFilterConfiguration` object.

        .. note::
            PyHanko implements legacy security handlers (which, according to
            the standard, aren't crypt filter-aware) using crypt filters
            as well, even though they aren't serialised to the output file.
    :param encrypt_metadata:
        Flag indicating whether document (XMP) metadata is to be encrypted.

        .. warning::
            Currently, PyHanko does not manage metadata streams, so until
            that changes, it is the responsibility of the API user to mark
            metadata streams using the `/Identity` crypt filter as required.

            Nonetheless, the value of this flag is required in key derivation
            computations, so the security handler needs to know about it.
    :param compat_entries:
        Write deprecated but technically unnecessary configuration settings for
        compatibility with certain implementations.
    """

    __registered_subclasses: Dict[str, Type['SecurityHandler']] = dict()
    _known_crypt_filters = dict()

    def __init__(self, version: SecurityHandlerVersion, legacy_keylen,
                 crypt_filter_config: 'CryptFilterConfiguration',
                 encrypt_metadata=True, compat_entries=True):
        self.version = version
        crypt_filter_config.set_security_handler(self)

        self.keylen = version.check_key_length(legacy_keylen)
        self.crypt_filter_config = crypt_filter_config
        self.encrypt_metadata = encrypt_metadata
        self._compat_entries = compat_entries
        self._credential = None

    def __init_subclass__(cls, **kwargs):
        # ensure that _known_crypt_filters is initialised to a fresh object
        # (to ensure that registering new crypt filters with subclasses doesn't
        # affect other classes in the hierarchy)
        if '_known_crypt_filters' not in cls.__dict__:
            cls._known_crypt_filters = dict(cls._known_crypt_filters)

    @staticmethod
    def register(cls: Type['SecurityHandler']):
        """
        Register a security handler class.
        Intended to be used as a decorator on subclasses.

        See :meth:`build` for further information.

        :param cls:
            A subclass of :class:`.SecurityHandler`.
        """
        # don't put this in __init_subclass__, so that people can inherit from
        # security handlers if they want
        SecurityHandler.__registered_subclasses[cls.get_name()] = cls
        return cls

    @staticmethod
    def build(encrypt_dict: generic.DictionaryObject) -> 'SecurityHandler':
        """
        Instantiate an appropriate :class:`.SecurityHandler` from a PDF
        document's encryption dictionary.

        PyHanko will search the registry for a security handler with
        a name matching the ``/Filter`` entry. Failing that, a security
        handler implementing the protocol designated by the
        ``/SubFilter`` entry (see :meth:`support_generic_subfilters`) will be
        chosen.

        Once an appropriate :class:`.SecurityHandler` subclass has been
        selected, pyHanko will invoke the subclass's
        :meth:`instantiate_from_pdf_object` method with the original encryption
        dictionary as its argument.

        :param encrypt_dict:
            A PDF encryption dictionary.
        :return:
        """
        handler_name = encrypt_dict.get('/Filter', '/Standard')
        try:
            cls = SecurityHandler.__registered_subclasses[handler_name]
        except KeyError:
            # no handler with that exact name, but if the encryption dictionary
            # specifies a generic /SubFilter, we can still try to look for an
            # alternative.
            try:
                subfilter = encrypt_dict['/SubFilter']
            except KeyError:
                raise misc.PdfReadError(
                    f"There is no security handler named {handler_name}, "
                    f"and the encryption dictionary does not contain a generic "
                    f"/SubFilter entry."
                )
            try:
                cls = next(
                    h for h in SecurityHandler.__registered_subclasses.values()
                    if subfilter in h.support_generic_subfilters()
                )
            except StopIteration:
                raise misc.PdfReadError(
                    f"There is no security handler named {handler_name}, and "
                    f"none of the available handlers support the declared "
                    f"/SubFilter {subfilter}."
                )

        return cls.instantiate_from_pdf_object(encrypt_dict)

    @classmethod
    def get_name(cls) -> str:
        """
        Retrieves the name of this security handler.

        :return:
            The name of this security handler.
        """
        raise NotImplementedError

    def extract_credential(self) -> Optional[SerialisableCredential]:
        """
        Extract a serialisable credential for later use, if the security handler
        supports it. It should allow the security handler to be unlocked
        with the same access level as the current one.

        :return:
            A serialisable credential, or ``None``.
        """
        if isinstance(self._credential, SerialisableCredential):
            return self._credential
        else:
            # This can mean several things: either the security handler doesn't
            # support credential serialisation at all, the particular credential
            # type can't be serialised, or the mode of operation doesn't permit
            # credential serialisation (e.g. pubkey security handler when the
            # private key is not available to the file writer)
            return None

    @classmethod
    def support_generic_subfilters(cls) -> Set[str]:
        """
        Indicates the generic ``/SubFilter`` values that this security handler
        supports.

        :return:
            A set of generic protocols (indicated in the ``/SubFilter`` entry
            of an encryption dictionary) that this :class:`.SecurityHandler`
            class implements. Defaults to the empty set.
        """
        return set()

    @classmethod
    def instantiate_from_pdf_object(cls,
                                    encrypt_dict: generic.DictionaryObject):
        """
        Instantiate an object of this class using a PDF encryption dictionary
        as input.

        :param encrypt_dict:
            A PDF encryption dictionary.
        :return:
        """
        raise NotImplementedError

    def is_authenticated(self) -> bool:
        """
        Return ``True`` if the security handler has been successfully
        authenticated against for document encryption purposes.

        The default implementation just attempts to call
        :meth:`get_file_encryption_key` and returns ``True`` if that doesn't
        raise an error.
        """
        try:
            self.get_file_encryption_key()
            return True
        except PdfKeyNotAvailableError:
            return False

    def as_pdf_object(self) -> generic.DictionaryObject:
        """
        Serialise this security handler to a PDF encryption dictionary.

        :return:
            A PDF encryption dictionary.
        """
        raise NotImplementedError

    def authenticate(self, credential, id1=None) -> AuthResult:
        """
        Authenticate a credential holder with this security handler.

        :param credential:
            A credential.
            The type of the credential is left up to the subclasses.
        :param id1:
            The first part of the document ID of the document being accessed.
        :return:
            An :class:`AuthResult` object indicating the level of access
            obtained.
        """
        raise NotImplementedError

    def get_string_filter(self) -> 'CryptFilter':
        """
        :return:
            The crypt filter responsible for decrypting strings
            for this security handler.
        """
        return self.crypt_filter_config.get_for_string()

    def get_stream_filter(self, name=None) -> 'CryptFilter':
        """
        :param name:
            Optionally specify a crypt filter by name.
        :return:
            The default crypt filter responsible for decrypting streams
            for this security handler, or the crypt filter named ``name``,
            if not ``None``.
        """
        if name is None:
            return self.crypt_filter_config.get_for_stream()
        return self.crypt_filter_config[name]

    def get_embedded_file_filter(self):
        """
        :return:
            The crypt filter responsible for decrypting embedded files
            for this security handler.
        """
        return self.crypt_filter_config.get_for_embedded_file()

    def get_file_encryption_key(self) -> bytes:
        """
        Retrieve the global file encryption key (used for streams and/or
        strings). If there is no such thing, or the key is not available,
        an error should be raised.

        :raise PdfKeyNotAvailableError: when the key is not available
        """
        raise NotImplementedError

    @classmethod
    def read_cf_dictionary(cls, cfdict: generic.DictionaryObject,
                           acts_as_default: bool) -> Optional['CryptFilter']:
        """
        Interpret a crypt filter dictionary for this type of security handler.

        :param cfdict:
            A crypt filter dictionary.
        :param acts_as_default:
            Indicates whether this filter is intended to be used in
            ``/StrF`` or ``/StmF``.
        :return:
            An appropriate :class:`.CryptFilter` object, or ``None``
            if the crypt filter uses the ``/None`` method.
        :raise NotImplementedError:
            Raised when the crypt filter's ``/CFM`` entry indicates an unknown
            crypt filter method.
        """
        # TODO does a V4 handler default to /Identity unless the /Encrypt
        #  dictionary specifies a custom filter?
        return build_crypt_filter(
            cls._known_crypt_filters, cfdict, acts_as_default
        )

    @classmethod
    def process_crypt_filters(cls, encrypt_dict: generic.DictionaryObject) \
            -> Optional['CryptFilterConfiguration']:

        stmf = encrypt_dict.get('/StmF', IDENTITY)
        strf = encrypt_dict.get('/StrF', IDENTITY)
        eff = encrypt_dict.get('/EFF', stmf)

        try:
            cf_config_dict = encrypt_dict['/CF']
        except KeyError:
            return None

        crypt_filters = {
            name: cls.read_cf_dictionary(cfdict, name in (stmf, strf))
            for name, cfdict in cf_config_dict.items()
        }
        return CryptFilterConfiguration(
            crypt_filters=crypt_filters, default_stream_filter=stmf,
            default_string_filter=strf, default_file_filter=eff
        )

    @classmethod
    def register_crypt_filter(cls, method: generic.NameObject,
                              factory: 'CryptFilterBuilder'):
        cls._known_crypt_filters[method] = factory


class CryptFilter:
    """
    Generic abstract crypt filter class.

    The superclass only handles the binding with the security handler, and
    offers some default implementations for serialisation routines that may
    be overridden in subclasses.

    There is generally no requirement for crypt filters to be compatible with
    *any* security handler (the leaf classes in this module aren't), but
    the API supports mixin usage so code can be shared.
    """

    _handler: 'SecurityHandler' = None
    _shared_key: Optional[bytes] = None
    _embedded_only = False

    def _set_security_handler(self, handler):
        """
        Set the security handler to which this crypt filter is tied.

        Called by pyHanko during initialisation.
        """
        self._handler = handler
        self._shared_key = None

    @property
    def _auth_failed(self) -> bool:
        """
        Indicate whether authentication previously failed for this crypt filter.

        Note that re-authenticating is not forbidden, this function mostly
        exists to make error reporting easier.

        Crypt filters are allowed to manage their own authentication, but may
        defer to the security handler as well.
        """
        raise NotImplementedError

    @property
    def method(self) -> generic.NameObject:
        """
        :return:
            The method name (``/CFM`` entry) associated with this crypt filter.
        """
        raise NotImplementedError

    @property
    def keylen(self) -> int:
        """
        :return:
            The keylength (in bytes) of the key associated with this crypt
            filter.
        """
        raise NotImplementedError

    def encrypt(self, key, plaintext: bytes, params=None) -> bytes:
        """
        Encrypt plaintext with the specified key.

        :param key:
            The current local key, which may or may not be equal to this
            crypt filter's global key.
        :param plaintext:
            Plaintext to encrypt.
        :param params:
            Optional parameters private to the crypt filter,
            specified as a PDF dictionary. These can only be used for
            explicit crypt filters; the parameters are then sourced from
            the corresponding entry in ``/DecodeParms``.
        :return:
            The resulting ciphertext.
        """
        raise NotImplementedError

    def decrypt(self, key, ciphertext: bytes, params=None) -> bytes:
        """
        Decrypt ciphertext with the specified key.

        :param key:
            The current local key, which may or may not be equal to this
            crypt filter's global key.
        :param ciphertext:
            Ciphertext to decrypt.
        :param params:
            Optional parameters private to the crypt filter,
            specified as a PDF dictionary. These can only be used for
            explicit crypt filters; the parameters are then sourced from
            the corresponding entry in ``/DecodeParms``.
        :return:
            The resulting plaintext.
        """
        raise NotImplementedError

    def as_pdf_object(self) -> generic.DictionaryObject:
        """
        Serialise this crypt filter to a PDF crypt filter dictionary.

        .. note::
            Implementations are encouraged to use a cooperative inheritance
            model, where subclasses first call ``super().as_pdf_object()``
            and add the keys they need before returning the result.

            This makes it easy to write crypt filter mixins that can provide
            functionality to multiple handlers.

        :return:
            A PDF crypt filter dictionary.
        """
        result = generic.DictionaryObject({
            # TODO handle /AuthEvent properly
            generic.NameObject('/AuthEvent'): (
                generic.NameObject('/EFOpen') if self._embedded_only
                else generic.NameObject('/DocOpen')
            ),
            generic.NameObject('/CFM'): self.method
        })
        return result

    def derive_shared_encryption_key(self) -> bytes:
        """
        Compute the (global) file encryption key for this crypt filter.

        :return:
            The key, as a :class:`bytes` object.
        :raise misc.PdfError:
            Raised if the data needed to derive the key is not present (e.g.
            because the caller hasn't authenticated yet).
        """
        raise NotImplementedError

    def derive_object_key(self, idnum, generation) -> bytes:
        """
        Derive the encryption key for a specific object, based on the shared
        file encryption key.

        :param idnum:
            ID of the object being encrypted.
        :param generation:
            Generation number of the object being encrypted.
        :return:
            The local key to use for this object.
        """
        raise NotImplementedError

    def set_embedded_only(self):
        self._embedded_only = True

    @property
    def shared_key(self) -> bytes:
        """
        Return the shared file encryption key for this crypt filter, or
        attempt to compute it using :meth:`derive_shared_encryption_key`
        if not available.
        """
        key = self._shared_key
        if key is None:
            if self._auth_failed:
                raise PdfKeyNotAvailableError("Authentication failed")
            key = self._shared_key = self.derive_shared_encryption_key()
        return key


class IdentityCryptFilter(CryptFilter, metaclass=misc.Singleton):
    """
    Class implementing the trivial crypt filter.

    This is a singleton class, so all its instances are identical.
    Additionally, some of the :class:`.CryptFilter` API is nonfunctional.
    In particular, :meth:`as_pdf_object` always raises an error, since the
    ``/Identity`` filter cannot be serialised.
    """

    method = generic.NameObject('/None')
    keylen = 0
    _auth_failed = False

    def derive_shared_encryption_key(self) -> bytes:
        """Always returns an empty byte string."""
        return b''

    def derive_object_key(self, idnum, generation) -> bytes:
        """
        Always returns an empty byte string.

        :param idnum:
            Ignored.
        :param generation:
            Ignored.
        :return:
        """
        return b''

    def _set_security_handler(self, handler):
        """
        No-op.

        :param handler:
            Ignored.
        :return:
        """
        return

    def as_pdf_object(self):
        """
        Not implemented for this crypt filter.

        :raise misc.PdfError:
            Always.
        """
        raise misc.PdfError("Identity filter cannot be serialised")

    def encrypt(self, key, plaintext: bytes, params=None) -> bytes:
        """
        Identity function.

        :param key:
            Ignored.
        :param plaintext:
            Returned as-is.
        :param params:
            Ignored.
        :return:
            The original plaintext.
        """
        return plaintext

    def decrypt(self, key, ciphertext: bytes, params=None) -> bytes:
        """
        Identity function.

        :param key:
            Ignored.
        :param ciphertext:
            Returned as-is.
        :param params:
            Ignored.
        :return:
            The original ciphertext.
        """
        return ciphertext


IDENTITY = generic.NameObject('/Identity')


class CryptFilterConfiguration:
    """
    Crypt filter store attached to a security handler.

    Instances of this class are not designed to be reusable.

    :param crypt_filters:
        A dictionary mapping names to their corresponding crypt filters.
    :param default_stream_filter:
        Name of the default crypt filter to use for streams.
    :param default_stream_filter:
        Name of the default crypt filter to use for strings.
    :param default_file_filter:
        Name of the default crypt filter to use for embedded files.

        .. note::
            PyHanko currently is not aware of embedded files, so managing these
            is the API user's responsibility.
    """

    def __init__(self, crypt_filters: Dict[str, CryptFilter] = None,
                 default_stream_filter=IDENTITY, default_string_filter=IDENTITY,
                 default_file_filter=None):
        def _select(name) -> CryptFilter:
            return (
                IdentityCryptFilter() if name == IDENTITY
                else crypt_filters[name]
            )

        self._crypt_filters = crypt_filters
        self._default_string_filter_name = default_string_filter
        self._default_stream_filter_name = default_stream_filter
        self._default_file_filter_name = default_file_filter
        self._default_stream_filter = _select(default_stream_filter)
        self._default_string_filter = _select(default_string_filter)
        default_file_filter = default_file_filter or default_stream_filter
        self._default_file_filter = _select(default_file_filter)

    def __getitem__(self, item):
        if item == generic.NameObject('/Identity'):
            return IdentityCryptFilter()
        return self._crypt_filters[item]

    def __contains__(self, item):
        return (
            item == generic.NameObject('/Identity')
            or item in self._crypt_filters
        )

    def filters(self):
        """Enumerate all crypt filters in this configuration."""
        return self._crypt_filters.values()

    def set_security_handler(self, handler: 'SecurityHandler'):
        """
        Set the security handler on all crypt filters in this configuration.

        :param handler:
            A :class:`.SecurityHandler` instance.
        """
        for cf in self._crypt_filters.values():
            cf._set_security_handler(handler)

    def get_for_stream(self):
        """
        Retrieve the default crypt filter to use with streams.

        :return:
            A :class:`.CryptFilter` instance.
        """
        return self._default_stream_filter

    def get_for_string(self):
        """
        Retrieve the default crypt filter to use with strings.

        :return:
            A :class:`.CryptFilter` instance.
        """
        return self._default_string_filter

    def get_for_embedded_file(self):
        """
        Retrieve the default crypt filter to use with embedded files.

        :return:
            A :class:`.CryptFilter` instance.
        """
        return self._default_file_filter

    @property
    def stream_filter_name(self) -> generic.NameObject:
        """
        The name of the default crypt filter to use with streams.
        """
        return self._default_stream_filter_name

    @property
    def string_filter_name(self) -> generic.NameObject:
        """
        The name of the default crypt filter to use with streams.
        """
        return self._default_string_filter_name

    @property
    def embedded_file_filter_name(self) -> generic.NameObject:
        """
        Retrieve the name of the default crypt filter to use with embedded
        files.
        """
        return self._default_file_filter_name

    def as_pdf_object(self):
        """
        Serialise this crypt filter configuration to a dictionary object,
        including all its subordinate crypt filters (with the exception of
        the identity filter, if relevant).
        """
        result = generic.DictionaryObject()
        result['/StmF'] = self._default_stream_filter_name
        result['/StrF'] = self._default_string_filter_name
        if self._default_file_filter_name is not None:
            result['/EFF'] = self._default_file_filter_name
        result['/CF'] = generic.DictionaryObject({
            generic.NameObject(key): value.as_pdf_object()
            for key, value in self._crypt_filters.items() if key != IDENTITY
        })
        return result

    def standard_filters(self):
        """
        Return the "standard" filters associated with this crypt filter
        configuration, i.e. those registered as the defaults for strings,
        streams and embedded files, respectively.

        These sometimes require special treatment (as per the specification).

        :return:
            A set with one, two or three elements.
        """
        stmf = self._default_stream_filter
        strf = self._default_string_filter
        eff = self._default_file_filter
        return {stmf, strf, eff}


CryptFilterBuilder = Callable[[generic.DictionaryObject, bool], CryptFilter]


def build_crypt_filter(reg: Dict[generic.NameObject, CryptFilterBuilder],
                       cfdict: generic.DictionaryObject,
                       acts_as_default: bool) -> Optional[CryptFilter]:
    """
    Interpret a crypt filter dictionary for a security handler.

    :param reg:
        A registry of named crypt filters.
    :param cfdict:
        A crypt filter dictionary.
    :param acts_as_default:
        Indicates whether this filter is intended to be used in
        ``/StrF`` or ``/StmF``.
    :return:
        An appropriate :class:`.CryptFilter` object, or ``None``
        if the crypt filter uses the ``/None`` method.
    :raise NotImplementedError:
        Raised when the crypt filter's ``/CFM`` entry indicates an unknown
        crypt filter method.
    """

    try:
        cfm = cfdict['/CFM']
    except KeyError:
        return None
    if cfm == '/None':
        return None
    try:
        factory = reg[cfm]
    except KeyError:
        raise NotImplementedError("No such crypt filter method: " + cfm)
    return factory(cfdict, acts_as_default)


ALL_PERMS = -4
"""
Dummy value that translates to "everything is allowed" in an
encrypted PDF document.
"""
