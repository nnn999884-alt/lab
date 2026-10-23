from pennybot.filing_detail import extract, form4, summary

RD = ("XYZ Therapeutics, Inc. today announced that it has entered into a securities purchase agreement with a single "
      "institutional investor for the purchase and sale of 2,500,000 shares of common stock (or pre-funded warrants in lieu "
      "thereof) at a purchase price of $0.80 per share in a registered direct offering. In a concurrent private placement, the "
      "company will also issue unregistered warrants to purchase up to 2,500,000 shares of common stock with an exercise price "
      "of $1.05 per share. H.C. Wainwright & Co. is acting as the exclusive placement agent. The offering is expected to close "
      "on or about October 24, 2026. Gross proceeds are expected to be approximately $2.0 million.")
NOTICE = ("On October 20, 2026, the Company received a deficiency letter from the Listing Qualifications Department of The Nasdaq "
          "Stock Market notifying the Company that the closing bid price was below the minimum bid price of $1.00 per share. "
          "The Company has 180 calendar days, or until April 19, 2027, to regain compliance.")
SPLIT = ("The Board approved a 1-for-25 reverse stock split of the common stock, which will become effective at 12:01 a.m. "
         "Eastern Time on November 3, 2026.")
F4 = """<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>DOE JOHN</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle>
</reportingOwnerRelationship></reportingOwner><nonDerivativeTable>
<nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>50000</value></transactionShares><transactionPricePerShare><value>0.42</value>
</transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""


def test_registered_direct_details():
    d = extract("8-K", "1.01,3.02,9.01", RD, amount=2.0e6)
    assert d["items"] == ["1.01 중요 계약 체결", "3.02 미등록 주식 발행(사모)"]         # 9.01 첨부는 뺀다
    assert d["price"] == 0.80 and d["shares"] == 2_500_000
    assert d["warrants"] == 2_500_000 and d["warrant_price"] == 1.05 and d["prefunded"]
    assert d["agent"] == "H.C. Wainwright" and d["closing"] == "October 24, 2026" and d["gross"] == 2.0e6
    assert d["headline"].startswith("it has entered into a securities purchase agreement")
    s = summary("8-K", d, market_price=1.00)
    assert s == ("$2.0M · 주당 $0.8000 (현재가 대비 -20%) · 2.5M주 · 워런트 2.5M주 @ $1.05 · 사전납입 워런트 포함 · "
                 "H.C. Wainwright · 종결 October 24, 2026")


def test_delisting_notice_and_reverse_split():
    d = extract("8-K", "3.01", NOTICE)
    assert d["deficiency"] == ["최저호가 $1 미달"] and d["deadline"] == "April 19, 2027"
    assert "최저호가 $1 미달 → 기한 April 19, 2027" in summary("8-K", d)
    d = extract("8-K", "5.03", SPLIT)
    assert d["split"] == "1:25" and d["split_effective"] == "November 3, 2026"
    assert summary("8-K", d).endswith("액면병합 1:25 (November 3, 2026 효력)")


def test_nothing_found_stays_empty():
    assert extract("8-K", "", "The company held its annual meeting.") == {}
    assert summary("8-K", {}) == ""


def test_form4():
    d = form4(F4)
    assert d["owner"] == "Doe John" and d["role"] == "Chief Executive Officer"
    assert d["tx"][0]["ko"] == "장내 매수" and d["net_buy_usd"] == 50000 * 0.42
    assert summary("4", d) == "Chief Executive Officer Doe John: 장내 매수 50K주 @ $0.4200 ($21K)"
    assert form4("<not xml") == {}
