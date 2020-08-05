import codecs
import hashlib
import logging
from datetime import datetime
from dataclasses import dataclass
from enum import IntEnum
from io import BytesIO
from typing import List

import tzlocal
from PyPDF2 import generic
from asn1crypto import cms, x509, algos, core, keys
from oscrypto import asymmetric, keys as oskeys

from pdf_utils.incremental_writer import IncrementalPdfFileWriter

pdf_name = generic.NameObject
pdf_string = generic.createStringObject

logger = logging.getLogger(__name__)


def pdf_utf16be_string(s):
    return pdf_string(codecs.BOM_UTF16_BE + s.encode('utf-16be'))


ASN_DT_FORMAT = "D:%Y%m%d%H%M%S"


def pdf_date(dt: datetime):
    base_dt = dt.strftime(ASN_DT_FORMAT)
    utc_offset_string = ''
    if dt.tzinfo is not None:
        # compute UTC off set string
        tz_seconds = dt.utcoffset().seconds
        if not tz_seconds:
            utc_offset_string = 'Z'
        else:
            sign = '+'
            if tz_seconds < 0:
                sign = '-'
                tz_seconds = abs(tz_seconds)
            hrs, tz_seconds = divmod(tz_seconds, 3600)
            mins = tz_seconds // 60
            # XXX the apostrophe after the minute part of the offset is NOT
            #  what's in the spec, but Adobe Reader DC refuses to validate
            #  signatures with a date string that doesn't contain it.
            #  No idea why.
            utc_offset_string = sign + ("%02d'%02d'" % (hrs, mins))

    return pdf_string(base_dt + utc_offset_string)


class SigByteRangeObject(generic.PdfObject):

    def __init__(self):
        self._filled = False
        self._range_object_offset = None
        self.first_region_len = 0
        self.second_region_offset = 0
        self.second_region_len = 0

    def fill_offsets(self, stream, sig_start, sig_end, eof):
        if self._filled:
            raise ValueError('Offsets already filled')
        if self._range_object_offset is None:
            raise ValueError(
                'Could not determine where to write /ByteRange value'
            )

        old_seek = stream.tell()
        self.first_region_len = sig_start
        self.second_region_offset = sig_end
        self.second_region_len = eof - sig_end
        # our ArrayObject is rigged to have fixed width
        # so we can just write over it

        stream.seek(self._range_object_offset)
        self.writeToStream(stream, None)

        stream.seek(old_seek)

    # noinspection PyPep8Naming, PyUnusedLocal
    def writeToStream(self, stream, encryption_key):
        if self._range_object_offset is None:
            self._range_object_offset = stream.tell()
        string_repr = "[ %08d %08d %08d %08d ]" % (
            0, self.first_region_len,
            self.second_region_offset, self.second_region_len,
        )
        stream.write(string_repr.encode('ascii'))


class PKCS7Placeholder(generic.PdfObject):

    # FIXME I have no idea what a reasonable size would be
    #  Write a "fake" signature first?
    def __init__(self, bytes_reserved=8192):
        self._placeholder = True
        self.value = b'0' * bytes_reserved
        self._offsets = None

    def fill_signature(self):
        self._placeholder = False
        # TODO implement

    @property
    def offsets(self):
        if self._offsets is None:
            raise ValueError('No offsets available')
        return self._offsets

    @property
    def original_bytes(self):
        return self.value

    # always ignore encryption key
    # (I think this is correct, but testing is required)
    # noinspection PyPep8Naming, PyUnusedLocal
    def writeToStream(self, stream, encryption_key):
        start = stream.tell()
        stream.write(b'<')
        stream.write(self.value)
        stream.write(b'>')
        end = stream.tell()
        if self._offsets is None:
            self._offsets = start, end


# simple PDF signature with two digested regions
# (pre- and post content)
class SignatureObject(generic.DictionaryObject):
    # TODO handle date encoding here as well
    def __init__(self, name, location, reason, timestamp: datetime):
        # initialise signature object
        super().__init__(
            {
                pdf_name('/Type'): pdf_name('/Sig'),
                pdf_name('/Filter'): pdf_name('/Adobe.PPKLite'),
                pdf_name('/SubFilter'): pdf_name('/adbe.pkcs7.detached'),
                pdf_name('/Name'): pdf_string(name),
                pdf_name('/Location'): pdf_string(location),
                pdf_name('/Reason'): pdf_string(reason),
                pdf_name('/M'): pdf_date(timestamp)
            }
        )
        # initialise placeholders for /Contents and /ByteRange
        pkcs7 = PKCS7Placeholder()
        self[pdf_name('/Contents')] = self.signature_contents = pkcs7
        byte_range = SigByteRangeObject()
        self[pdf_name('/ByteRange')] = self.byte_range = byte_range


def _simple_box_appearance(box): 
    # box should have four coordinates: x1 y1 x2 y2
    x1, y1, x2, y2 = box
    box_width = abs(x1 - x2)
    box_height = abs(y1 - y2)
    if len(box) != 4:
        raise ValueError('Box should have four coordinates')
    rect = list(map(generic.FloatObject, box))
    command_stream = ' '.join([
        'q',  # save
        '0.3 0.3 0.3 rg',  # set fill colour
        # set up rectangle path
        '0 %.4f %.4f %.4f re' % (box_height, box_width, -box_height),
        'f',  # fill stroke
        'Q'  # restore graphics state
    ]).encode('ascii')
    # we'll only set normal appearance, nothing else
    normal_appearance = generic.StreamObject.initializeFromDictionary({
        '__streamdata__': command_stream,
        # PyPDF2 is bizarre about /Length. It requires the /Length attribute
        #  in initializeFromDictionary (because deleting it produces a KeyError)
        #  but then simply recomputes it as needed.
        pdf_name('/Length'): len(command_stream),
        pdf_name('/BBox'): generic.ArrayObject(list(
            map(generic.FloatObject, (0.0, box_height, box_width, 0.0))
        )),
        # TODO this will change when we involve text etc.
        pdf_name('/Resources'): generic.DictionaryObject(),
        pdf_name('/Type'): pdf_name('/XObject'),
        pdf_name('/Subtype'): pdf_name('/Form')
    })
    return rect, normal_appearance


def register_annotation(page_ref, annot_ref, writer: IncrementalPdfFileWriter):
    page_obj = page_ref.getObject()
    try:
        annots_ref = page_obj.raw_get('/Annots')
        if isinstance(annots_ref, generic.IndirectObject):
            annots = annots_ref.getObject()
            writer.mark_update(annot_ref)
        else:
            # we need to update the entire page object if the annots array
            # is a direct object
            annots = annots_ref
            writer.mark_update(page_ref)
    except KeyError:
        annots = generic.ArrayObject()
        writer.mark_update(page_ref)
        page_obj[pdf_name('/Annots')] = annots

    annots.append(annot_ref)


class SignatureFormField(generic.DictionaryObject):
    def __init__(self, field_name, include_on_page, *, writer,
                 sig_object_ref=None, box=None):
        if box is not None:
            visible = True
            rect, normal_app = _simple_box_appearance(box)
            normal_app_ref = writer.add_object(normal_app)
            ap = generic.DictionaryObject({pdf_name('/N'): normal_app_ref})
        else:
            rect = [generic.FloatObject(0.0)] * 4
            ap = None
            visible = False

        # this sets the "Print" bit, and activates "Locked" if the
        # signature field is ready to be filled
        flags = 0b100 if sig_object_ref is None else 0b10000100
        super().__init__({
            # Signature field properties
            pdf_name('/FT'): pdf_name('/Sig'),
            pdf_name('/T'): pdf_utf16be_string(field_name),
            # Annotation properties: bare minimum
            pdf_name('/Type'): pdf_name('/Annot'),
            pdf_name('/Subtype'): pdf_name('/Widget'),
            pdf_name('/F'): generic.NumberObject(flags),
            pdf_name('/P'): include_on_page,
            pdf_name('/Rect'): generic.ArrayObject(rect)
        })
        if sig_object_ref is not None:
            self[pdf_name('/V')] = sig_object_ref
        if ap is not None:
            self[pdf_name('/AP')] = ap

        # register ourselves
        self.reference = self_reference = writer.add_object(self)
        # if we're building an invisible form field, this is all there is to it
        if visible:
            register_annotation(include_on_page, self_reference, writer)


def simple_cms_attribute(attr_type, value):
    return cms.CMSAttribute({
        'type': cms.CMSAttributeType(attr_type),
        'values': (value,)
    })


@dataclass(frozen=True)
class Signer:
    signing_cert: x509.Certificate
    ca_chain: List[x509.Certificate]
    signing_key: keys.PrivateKeyInfo
    validity_window: (datetime, datetime)

    def sign(self, data_digest: bytes, digest_algorithm: str,
             timestamp: datetime = None) -> bytes:

        # Implementation loosely based on similar functionality in
        # https://github.com/m32/endesive/.

        digest_algorithm_obj = algos.DigestAlgorithm(
            {'algorithm': digest_algorithm}
        )

        timestamp = timestamp or datetime.now(tz=tzlocal.get_localzone())
        signed_attrs = cms.CMSAttributes([
            simple_cms_attribute('content_type', 'data'),
            simple_cms_attribute('message_digest', data_digest),
            # TODO support using timestamping servers
            # TODO The spec actually mandates that the timestamp be
            #  an unauthenticated attribute if present. This is how JSignPDF
            #  does it, though, so meh.
            #  Anyway, doing this properly in the way mandated by RFC 3161
            #  Appendix A is a little more involved.
            simple_cms_attribute(
                'signing_time', cms.Time({'utc_time': core.UTCTime(timestamp)})
            )
            # TODO support adding Adobe-style revocation information
        ])

        # the piece of data we'll actually sign is a DER-encoded version of the
        # signed attributes of our message
        signature = asymmetric.rsa_pkcs1v15_sign(
            asymmetric.load_private_key(self.signing_key),
            signed_attrs.dump(),
            digest_algorithm.lower()
        )

        # build the signer info object that goes into the PKCS7 signature
        # (see RFC 2315 § 9.2)
        signer_info = cms.SignerInfo({
            'version': 'v1',
            'sid': cms.SignerIdentifier({
                'issuer_and_serial_number': cms.IssuerAndSerialNumber({
                    'issuer': self.signing_cert.issuer,
                    'serial_number': self.signing_cert.serial_number,
                })
            }),
            'digest_algorithm': digest_algorithm_obj,
            # TODO implement PSS & HSM support (PKCS11 devices)
            'signature_algorithm': algos.SignedDigestAlgorithm(
                {'algorithm': 'rsassa_pkcs1v15'}
            ),
            'signed_attrs': signed_attrs,
            'signature': signature
        })

        # this is the SignedData object for our message (see RFC 2315 § 9.1)
        signed_data = {
            'version': 'v1',
            'digest_algorithms': cms.DigestAlgorithms((digest_algorithm_obj,)),
            'encap_content_info': {'content_type': 'data'},
            'certificates': [self.signing_cert] + self.ca_chain,
            'signer_infos': [signer_info]
        }

        # time to pack up
        message = cms.ContentInfo({
            'content_type': cms.ContentType('signed_data'),
            'content': cms.SignedData(signed_data)
        })

        return message.dump()

    @classmethod
    def load(cls, key_file, cert_file, key_passphrase=None):
        try:
            # load cryptographic data (both PEM and DER are supported)
            with open(key_file, 'rb') as f:
                signing_key: keys.PrivateKeyInfo = oskeys.parse_private(
                    f.read(), password=key_passphrase
                )
            with open(cert_file, 'rb') as f:
                signing_cert: x509.Certificate = oskeys.parse_certificate(
                    f.read()
                )
        except (IOError, ValueError) as e:
            logger.error('Could not load cryptographic material', e)
            return None
        valid_from = signing_cert.not_valid_before
        valid_until = signing_cert.not_valid_after

        return Signer(
            signing_cert=signing_cert, signing_key=signing_key,
            validity_window=(valid_from, valid_until),
            ca_chain=[]  # TODO implement this
        )


# TODO add more customisability

@dataclass(frozen=True)
class SigFieldSpec:
    sig_field_name: str
    on_page: int = 0
    box: (int, int, int, int) = None


class DocMDPPerm(IntEnum):
    """
    Cf. Table 254  in ISO 32000
    """

    NO_CHANGES = 0
    FILL_FORMS = 2
    ANNOTATE = 3


@dataclass(frozen=True)
class PdfSignatureMetadata:
    name: str
    location: str
    reason: str
    field_name: str
    certify: bool = False
    # only relevant for certification
    docmdp_permissions: DocMDPPerm = DocMDPPerm.FILL_FORMS


def _find_sig_field(form, sig_field_name):

    utf16be_name = pdf_utf16be_string(sig_field_name)
    try:
        # grab the array of form field references
        fields = form['/Fields']
        # check if a signature field with the requested name exists
        for field_ref in fields:
            field = field_ref.getObject()
            if field.raw_get('/T') == utf16be_name:
                if field['/FT'] != pdf_name('/Sig'):
                    raise ValueError(
                        'A field with name %s exists but is not a signature '
                        'field' % sig_field_name
                    )
                elif '/V' in field:
                    raise ValueError(
                        'The field with name %s appears to be filled already'
                        % sig_field_name
                    )
                return field_ref, fields
    except KeyError:
        # in a well-formed PDF, this should never happen
        fields = generic.ArrayObject()
    # no corresponding field found, need to create it later
    return None, fields


def _certification_setup(writer: IncrementalPdfFileWriter,
                         sig_obj_ref, md_algorithm, permission_level):
    """
    Cf. Tables 252, 253 and 254 in ISO 32000
    """
    transform_params = generic.DictionaryObject({
        pdf_name('/Type'): pdf_name('/TransformParams'),
        pdf_name('/V'): pdf_name('/1.2'),
        pdf_name('/P'): generic.NumberObject(permission_level)
    })
    tp_ref = writer.add_object(transform_params)

    # not to be confused with our indirect reference *to* the signature object--
    # this is part of the /Reference entry of the signature object.
    sigref_object = generic.DictionaryObject({
        pdf_name('/Type'): pdf_name('/SigRef'),
        pdf_name('/TransformMethod'): pdf_name('/DocMDP'),
        pdf_name('/DigestMethod'): pdf_name('/' + md_algorithm.upper()),
        pdf_name('/TransformParams'): tp_ref
    })

    # after preparing the sigref object, insert it into the actual signature
    # object under /Reference (for some reason this is supposed to be an array)
    sigref_list = generic.ArrayObject([writer.add_object(sigref_object)])
    sig_obj_ref.getObject()[pdf_name('/Reference')] = sigref_list

    # finally, register a /DocMDP permission entry in the document catalog
    root = writer.root
    # the usual song and dance to grab a reference to /Perms, or create it
    # TODO I've done this enough times to factor it out, I suppose
    try:
        perms_ref = root.raw_get('/Perms')
        if isinstance(perms_ref, generic.IndirectObject):
            perms = perms_ref.getObject()
            writer.mark_update(perms_ref)
        else:
            perms = perms_ref
            writer.update_root()
    except KeyError:
        root[pdf_name('/Perms')] = perms = generic.DictionaryObject()
        writer.update_root()
    perms[pdf_name('/DocMDP')] = sig_obj_ref


def _prepare_sig_field(sig_field_name, root,
                       update_writer: IncrementalPdfFileWriter = None,
                       existing_fields_only=False, lock_sig_flags=True, 
                       **kwargs):

    if not existing_fields_only and update_writer is None:
        raise ValueError(
            'Adding form fields requires a writer to process updates'
        )

    try:
        form_ref = root.raw_get('/AcroForm')
        form_created = False

        if isinstance(form_ref, generic.IndirectObject):
            # The /AcroForm exists and is indirect. Hence, we may need to write
            # an update if we end up having to add the signature field
            form = form_ref.getObject()
        else:
            # the form is a direct object, so we'll replace it with
            # an indirect one, and mark the root to be updated
            # (I think this is fairly rare, but requires testing!)
            form = form_ref
            # if updates are active, we forgo the replacement
            #  operation; in this case, one should only update the
            #  referenced form field anyway.
            if update_writer is not None:
                # this creates a new xref
                form_created = True
                form_ref = update_writer.add_object(form)
                root[pdf_name('/AcroForm')] = form_ref
                update_writer.update_root()
        # try to extend the existing form object first
        # and mark it for an update if necessary
        sig_field_ref, fields = _find_sig_field(form, sig_field_name)
    except KeyError:
        if existing_fields_only:
            raise ValueError('This file does not contain a form.')
        # no AcroForm present, so create one
        form = generic.DictionaryObject()
        form_created = True
        root[pdf_name('/AcroForm')] = form_ref = update_writer.add_object(form)
        fields = generic.ArrayObject()
        # now we need to mark the root as updated
        update_writer.update_root()
        sig_field_ref = None

    field_created = sig_field_ref is None
    if field_created:
        # no signature field exists, so create one
        if existing_fields_only:
            raise ValueError('Could not find signature field')
        sig_form_kwargs = {
            'include_on_page': root['/Pages']['/Kids'][0],
        }
        sig_form_kwargs.update(**kwargs)
        sig_field = SignatureFormField(
            sig_field_name, writer=update_writer, **sig_form_kwargs
        )
        sig_field_ref = sig_field.reference
        fields.append(sig_field_ref)
        form[pdf_name('/Fields')] = fields

        # make sure /SigFlags is present. If not, create it
        sig_flags = 3 if lock_sig_flags else 1
        form.setdefault(pdf_name('/SigFlags'), generic.NumberObject(sig_flags))
        # if we're adding a field to an existing form, this requires
        # registering an update
        if not form_created:
            update_writer.mark_update(form_ref)

    return field_created, sig_field_ref


def append_signature_fields(input_handle, sig_field_specs: List[SigFieldSpec]):
    pdf_out = IncrementalPdfFileWriter(input_handle)
    root = pdf_out.root

    page_list = root['/Pages']['/Kids']
    for sp in sig_field_specs:
        field_created, _ = _prepare_sig_field(
            sp.sig_field_name, root, update_writer=pdf_out,
            existing_fields_only=False, box=sp.box,
            include_on_page=page_list[sp.on_page]
        )
        if not field_created:
            raise ValueError(
                'Signature field with name %s already exists.'
                % sp.sig_field_name
            )

    output = BytesIO()
    pdf_out.write(output)
    output.seek(0)
    return output


def sign_pdf(input_handle, signature_meta: PdfSignatureMetadata, signer: Signer,
             md_algorithm='sha1', existing_fields_only=False):

    # TODO generate an error when signatures are present and there's no
    #  open signature field to fill (since in that situation we cannot sign
    #  without invalidating the existing signatures) or DocMDP doesn't allow
    #  signing

    # TODO explicitly disallow multiple certification signatures

    # TODO force md_algorithm to agree with the certification signature
    #  if present

    # TODO allow signing an existing signature field without specifying the name

    pdf_out = IncrementalPdfFileWriter(input_handle)
    root = pdf_out.root

    timestamp = datetime.now(tz=tzlocal.get_localzone())
    # we need to add a signature object and a corresponding form field
    # to the PDF file
    sig_obj = SignatureObject(
        signature_meta.name, signature_meta.location,
        signature_meta.reason, timestamp
    )
    sig_obj_ref = pdf_out.add_object(sig_obj)

    # grab or create a sig field
    field_created, sig_field_ref = _prepare_sig_field(
        signature_meta.field_name, root, update_writer=pdf_out,
        existing_fields_only=existing_fields_only, lock_sig_flags=True
    )
    sig_field = sig_field_ref.getObject()
    # fill in a reference to the (empty) signature object
    sig_field[pdf_name('/V')] = sig_obj_ref

    if not field_created:
        # still need to mark it for updating
        pdf_out.mark_update(sig_field_ref)

    if signature_meta.certify:
        _certification_setup(
            pdf_out, sig_obj_ref, md_algorithm,
            signature_meta.docmdp_permissions
        )

    # Render the PDF to a byte buffer with placeholder values
    # for the signature data
    output = BytesIO()
    pdf_out.write(output)

    # retcon time: write the proper values of the /ByteRange entry
    #  in the signature object
    eof = output.tell()
    sig_start, sig_end = sig_obj.signature_contents.offsets
    sig_obj.byte_range.fill_offsets(output, sig_start, sig_end, eof)

    # compute the digests
    output_buffer = output.getbuffer()
    md = getattr(hashlib, md_algorithm)()
    # these are memoryviews, so slices should not copy stuff around
    md.update(output_buffer[:sig_start])
    md.update(output_buffer[sig_end:])
    output_buffer.release()

    signature = signer.sign(
        md.digest(), md_algorithm, timestamp=timestamp
    ).hex().encode('ascii')

    # +1 to skip the '<'
    output.seek(sig_start + 1)
    output.write(signature)

    output.seek(0)
    return output


def append_signature_fields_to_file(infile_name, outfile_name, *args):
    with open(infile_name, 'rb') as infile:
        result = append_signature_fields(infile, args)
    with open(outfile_name, 'wb') as outfile:
        buf = result.getbuffer()
        outfile.write(buf)
        buf.release()


def sign_pdf_file(infile_name, outfile_name,
                  signature_meta: PdfSignatureMetadata, key_file, cert_file,
                  key_passphrase, existing_fields_only=False):
    signer = Signer.load(
        cert_file=cert_file, key_file=key_file, key_passphrase=key_passphrase
    )
    with open(infile_name, 'rb') as infile:
        result = sign_pdf(
            infile, signature_meta, signer,
            existing_fields_only=existing_fields_only
        )
    with open(outfile_name, 'wb') as outfile:
        buf = result.getbuffer()
        outfile.write(buf)
        buf.release()
