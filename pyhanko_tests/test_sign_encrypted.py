from io import BytesIO

import pytest
from freezegun import freeze_time

from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import signers
from pyhanko_tests.samples import (
    MINIMAL_AES256,
    MINIMAL_ONE_FIELD_AES256,
    MINIMAL_ONE_FIELD_RC4,
    MINIMAL_PUBKEY_ONE_FIELD_AES256,
    MINIMAL_PUBKEY_ONE_FIELD_RC4,
    MINIMAL_RC4,
    PUBKEY_SELFSIGNED_DECRYPTER,
)
from pyhanko_tests.signing_commons import FROM_CA, val_trusted


@pytest.mark.parametrize('password', [b'usersecret', b'ownersecret'])
@freeze_time('2020-11-01')
def test_sign_crypt_rc4(password):
    w = IncrementalPdfFileWriter(BytesIO(MINIMAL_ONE_FIELD_RC4))
    w.encrypt(password)
    out = signers.sign_pdf(
        w, signers.PdfSignatureMetadata(), signer=FROM_CA,
        existing_fields_only=True
    )

    r = PdfFileReader(out)
    r.decrypt(password)
    s = r.embedded_signatures[0]
    val_trusted(s)


@pytest.mark.parametrize('password', ['usersecret', 'ownersecret'])
@freeze_time('2020-11-01')
def test_sign_crypt_aes256(password):
    w = IncrementalPdfFileWriter(BytesIO(MINIMAL_ONE_FIELD_AES256))
    w.encrypt(password)
    out = signers.sign_pdf(
        w, signers.PdfSignatureMetadata(), signer=FROM_CA,
        existing_fields_only=True
    )

    r = PdfFileReader(out)
    r.decrypt(password)
    s = r.embedded_signatures[0]
    val_trusted(s)


@freeze_time('2020-11-01')
def test_sign_crypt_pubkey_aes256():
    w = IncrementalPdfFileWriter(BytesIO(MINIMAL_PUBKEY_ONE_FIELD_AES256))
    w.encrypt_pubkey(PUBKEY_SELFSIGNED_DECRYPTER)
    out = signers.sign_pdf(
        w, signers.PdfSignatureMetadata(), signer=FROM_CA,
        existing_fields_only=True
    )

    r = PdfFileReader(out)
    r.decrypt_pubkey(PUBKEY_SELFSIGNED_DECRYPTER)
    s = r.embedded_signatures[0]
    val_trusted(s)


@freeze_time('2020-11-01')
def test_sign_crypt_pubkey_rc4():
    w = IncrementalPdfFileWriter(BytesIO(MINIMAL_PUBKEY_ONE_FIELD_RC4))
    w.encrypt_pubkey(PUBKEY_SELFSIGNED_DECRYPTER)
    out = signers.sign_pdf(
        w, signers.PdfSignatureMetadata(), signer=FROM_CA,
        existing_fields_only=True
    )

    r = PdfFileReader(out)
    r.decrypt_pubkey(PUBKEY_SELFSIGNED_DECRYPTER)
    s = r.embedded_signatures[0]
    val_trusted(s)


sign_crypt_rc4_new_params = [
    [b'usersecret', 0], [b'usersecret', 1],
    [b'ownersecret', 0], [b'ownersecret', 1]
]


@pytest.mark.parametrize('password, file', sign_crypt_rc4_new_params)
@freeze_time('2020-11-01')
def test_sign_crypt_rc4_new(password, file):
    w = IncrementalPdfFileWriter(BytesIO(sign_crypt_rc4_files[file]))
    w.encrypt(password)
    out = signers.sign_pdf(
        w, signers.PdfSignatureMetadata(field_name='SigNew'), signer=FROM_CA,
    )
    out.seek(0)
    r = PdfFileReader(out)
    r.decrypt(password)

    s = r.embedded_signatures[0]
    val_trusted(s)


@pytest.mark.parametrize('password, file', sign_crypt_rc4_new_params)
@freeze_time('2020-11-01')
def test_sign_crypt_aes256_new(password, file):
    w = IncrementalPdfFileWriter(BytesIO(sign_crypt_aes256_files[file]))
    w.encrypt(password)
    out = signers.sign_pdf(
        w, signers.PdfSignatureMetadata(field_name='SigNew'), signer=FROM_CA,
    )
    out.seek(0)
    r = PdfFileReader(out)
    r.decrypt(password)

    s = r.embedded_signatures[0]
    val_trusted(s)


sign_crypt_rc4_files = (MINIMAL_RC4, MINIMAL_ONE_FIELD_RC4)
sign_crypt_aes256_files = (MINIMAL_AES256, MINIMAL_ONE_FIELD_AES256)