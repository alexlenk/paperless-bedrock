from paperless_bedrock.identifiers import extract, normalize


def kinds(text: str) -> dict[str, str]:
    return {r.kind: r.value for r in extract(text)}


def test_extracts_common_reference_numbers() -> None:
    text = "\n".join(
        [
            "IBAN: DE89 3704 0044 0532 0130 00",
            "Gläubiger-ID: DE98ZZZ09999999999  Mandatsreferenz 4711",
            "USt-IdNr.: DE123456789",
            "Amtsgericht Mannheim HRB 123456",
            "Steuernummer 31131/42555",
            "Kundennummer: 1000-2345",
            "Versicherungsschein-Nr. KV 99887766",
            "Rechnungsnummer: 2250028431",
            "Aktenzeichen: 3 C 123/26",
        ]
    )
    found = kinds(text)
    assert found["iban"] == "DE89370400440532013000"
    assert found["creditor_id"] == "DE98ZZZ09999999999"
    assert found["vat_id"] == "DE123456789"
    assert found["commercial_register"] == "HRB123456"
    assert found["tax_number"] == "3113142555"
    assert found["customer_number"] == "10002345"
    assert found["contract_number"] == "KV99887766"
    assert found["invoice_number"] == "2250028431"
    assert found["case_reference"] == "3C12326"


def test_invalid_iban_and_iban_as_vat_are_ignored() -> None:
    found = extract("IBAN DE88 3704 0044 0532 0130 00\nUSt DE89370400440532013000")
    # line 1: checksum invalid -> ignored; line 2: a valid IBAN, not a VAT ID
    assert [(r.kind, r.value) for r in found] == [("iban", "DE89370400440532013000")]


def test_normalize() -> None:
    assert normalize(" de 89-3704/00 ") == "DE89370400"
