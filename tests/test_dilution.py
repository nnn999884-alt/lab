from datetime import date, timedelta

from pennybot.dilution import DilutionService, Event, build_profile, classify, parse_amount, pump_flags
from pennybot.screener import edgar as ed

D = date(2026, 10, 21)


def kinds(evs):
    return {(e.kind, e.direction) for e in evs}


def test_atm_termination_is_positive_only_in_current_reports():
    t8 = "<p>On October 20, 2026, the Company terminated the At The Market Offering Agreement with H.C. Wainwright.</p>"
    assert kinds(classify("8-K", "1.02", t8, D)) == {("ATM_TERMINATED", 1)}
    # ATM 설명서의 상투 문구 "will terminate upon" 은 종료가 아니다
    prospectus = ("The offering pursuant to the Sales Agreement will terminate upon the sale of all shares. "
                  "at-the-market offering with an aggregate offering price of up to $8,000,000")
    evs = classify("424B5", "", prospectus, D)
    assert kinds(evs) == {("ATM_NEW", -1)} and evs[0].amount == 8_000_000
    # 8-K 안에서도 "may terminate" 는 무시하고 실제 종료 문장만 잡는다
    t = "the Company may terminate the Sales Agreement at any time. On Oct 1 the Company terminated the Sales Agreement."
    assert ("ATM_TERMINATED", 1) in kinds(classify("8-K", "1.02", t, D))


def test_item_101_is_financing_when_spa_language():
    rd = ("entered into a securities purchase agreement with institutional investors for a registered direct "
          "offering of 2,000,000 shares for gross proceeds of approximately $4.5 million")
    evs = classify("8-K", "1.01,9.01", rd, D)
    assert ("REGISTERED_DIRECT", -1) in kinds(evs) and ("MATERIAL_CONTRACT", 1) not in kinds(evs)
    assert all(e.amount == 4_500_000 for e in evs if e.direction < 0)
    contract = "entered into a Master Supply Agreement with a hospital network for distribution of devices"
    assert kinds(classify("8-K", "1.01", contract, D)) == {("MATERIAL_CONTRACT", 1)}


def test_form_only_events_and_warrants():
    assert kinds(classify("RW", "", "", D)) == {("REGISTRATION_WITHDRAWN", 1)}
    assert ("SHELF_FILED", -1) in kinds(classify("S-3", "", "", D))
    w = "entered into warrant inducement agreements; the Company agreed to reduce the exercise price to $1.10"
    assert {("WARRANT_INDUCEMENT", -1), ("WARRANT_REPRICE", -1)} <= kinds(classify("8-K", "1.01", w, D))
    assert ("DELISTING_NOTICE", -1) in kinds(classify("8-K", "3.01", "", D))
    assert parse_amount("gross proceeds of $1.2 billion") == 1.2e9


def E(kind, d, days_ago, form="8-K", amount=None):
    return Event(kind, d, form, D - timedelta(days=days_ago), "a", amount)


def test_profile_scoring_and_baby_shelf():
    # ATM 진행 중 + 최근 증자 → 고위험
    p = build_profile([E("SHELF_FILED", -1, 200, "S-3"), E("ATM_NEW", -1, 100, "424B5"),
                       E("REGISTERED_DIRECT", -1, 10, amount=3e6)], D, market_cap=20e6)
    assert p.atm_active and p.bucket == "high" and p.days_since_financing == 10
    # ATM 이 종료되면 점수가 크게 내려간다 (사용자가 말한 'ATM 취소 → 상승' 상황)
    p2 = build_profile([E("SHELF_FILED", -1, 200, "S-3"), E("ATM_NEW", -1, 100, "424B5"),
                        E("ATM_TERMINATED", 1, 1)], D, market_cap=20e6)
    assert not p2.atm_active and "ATM_TERMINATED" in p2.recent_positive and p2.score < p.score - 40
    # baby shelf: 시총 2천만$ → 1/3 = 667만$ 한도, 12개월 내 600만$ 조달 → 남은 한도 적음
    p3 = build_profile([E("SHELF_FILED", -1, 300, "S-3"), E("TAKEDOWN", -1, 90, "424B5", amount=6e6)],
                       D, market_cap=20e6)
    assert p3.baby_shelf and p3.capacity_usd < 1e6 and any("한도 적음" in r for r in p3.reasons)
    # 아무것도 없으면 낮음
    assert build_profile([], D, 50e6).bucket == "low"


def test_pump_flags():
    f = pump_flags(list_date=D - timedelta(days=100), country="K3", shares_outstanding=4e6, today=D,
                   has_catalyst=False)
    assert len(f) == 4
    assert pump_flags(list_date=date(2015, 1, 1), country="NV", shares_outstanding=50e6, today=D,
                      has_catalyst=True) == []


class FakeEdgar:
    def __init__(self):
        self.doc_calls = 0

    def submissions(self, cik):
        return {"name": "ACME", "addresses": {"business": {"stateOrCountry": "F4"}},
                "filings": {"recent": {"form": ["8-K", "S-3", "4"], "filingDate": ["2026-10-20", "2026-01-05", "2026-10-01"],
                                       "accessionNumber": ["a1", "a2", "a3"],
                                       "primaryDocument": ["d.htm", "s3.htm", "f4.xml"], "items": ["1.02", "", ""]}}}

    def document(self, cik, f):
        self.doc_calls += 1
        return "the Company terminated the Equity Distribution Agreement with Roth" if f.form == "8-K" else ""


def test_service_caches_and_builds_profile(tmp_path):
    e = FakeEdgar()
    svc = DilutionService(e, tmp_path)
    p = svc.profile(1, D, market_cap=30e6)
    assert "ATM_TERMINATED" in p.recent_positive and p.shelf_active
    calls = e.doc_calls
    DilutionService(e, tmp_path).profile(1, D, 30e6)     # 디스크 캐시 → 본문 재요청 없음
    assert e.doc_calls == calls
    assert svc.company(1, D)["country"] == "F4"
    assert isinstance(ed.parse_submissions(e.submissions(1))[0], ed.Filing)


def test_old_shelf_found_even_with_many_recent_8ks(tmp_path):
    class Busy(FakeEdgar):
        def submissions(self, cik):
            n = 60
            return {"filings": {"recent": {
                "form": ["8-K"] * n + ["S-3"], "filingDate": ["2026-10-01"] * n + ["2024-03-01"],
                "accessionNumber": [f"b{i}" for i in range(n + 1)], "primaryDocument": ["d.htm"] * (n + 1),
                "items": ["8.01"] * n + [""]}}}

        def document(self, cik, f):
            return "nothing relevant"

    p = DilutionService(Busy(), tmp_path, max_texts=40).profile(1, D, 30e6)
    assert p.shelf_active


INDEX_HTML = """<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/1/000000000126000001/d8k.htm">d8k.htm</a></td><td>8-K</td><td>10</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1/000000000126000001/ex99-1.htm">ex99-1.htm</a></td><td>EX-99.1</td><td>5</td></tr>
<tr><td>3</td><td>SPA</td><td><a href="/Archives/edgar/data/1/000000000126000001/ex10-1.htm">ex10-1.htm</a></td><td>EX-10.1</td><td>50</td></tr>
</table>"""


def test_parse_index():
    docs = ed.parse_index(INDEX_HTML)
    assert docs[0] == ("8-K", "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/d8k.htm")
    assert [t for t, _ in docs] == ["8-K", "EX-99.1", "EX-10.1"]


class ExhibitEdgar(FakeEdgar):
    def submissions(self, cik):
        return {"filings": {"recent": {"form": ["8-K"], "filingDate": ["2026-10-20"], "accessionNumber": ["x-1"],
                                       "primaryDocument": ["d8k.htm"], "items": ["8.01,9.01"]}}}

    def document(self, cik, f):
        return "Item 8.01 Other Events. See press release attached as Exhibit 99.1."

    def exhibits(self, cik, f):
        return ed.parse_index(INDEX_HTML)

    def url_text(self, url):
        if "ex99" in url:
            return "<p>ACME announced today that it has terminated its At-The-Market sales agreement with Maxim.</p>"
        # 계약서: 해지 조항 문구 + 증권매매계약
        return "This Securities Purchase Agreement may be terminated ... the sales agreement shall terminate"


def test_exhibits_change_classification(tmp_path):
    evs = DilutionService(ExhibitEdgar(), tmp_path).events(1, D)
    kinds = {(e.kind, e.direction) for e in evs}
    assert ("ATM_TERMINATED", 1) in kinds                    # 보도자료(EX-99)에서 호재
    assert ("SECURITIES_PURCHASE_AGREEMENT", -1) in kinds    # 계약서(EX-10)에서 악재만
    assert sum(1 for k, d in kinds if k == "ATM_TERMINATED") == 1


def test_future_filings_ignored(tmp_path):
    e = FakeEdgar()
    svc = DilutionService(e, tmp_path)
    assert all(ev.filed <= date(2026, 6, 1) for ev in svc.events(1, date(2026, 6, 1)))


FACTS = {"facts": {"us-gaap": {
    "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
        {"end": "2026-03-31", "val": 6_000_000, "filed": "2026-05-10"},
        {"end": "2026-06-30", "val": 3_000_000, "filed": "2026-08-12"},
        {"end": "2026-09-30", "val": 900_000, "filed": "2026-11-12"}]}},          # 미래 제출분 (무시해야 함)
    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
        {"start": "2026-01-01", "end": "2026-06-30", "val": -6_000_000, "filed": "2026-08-12"},
        {"start": "2026-04-01", "end": "2026-06-30", "val": -3_000_000, "filed": "2026-08-12"}]}},
    "ClassOfWarrantOrRightOutstanding": {"units": {"shares": [{"end": "2026-06-30", "val": 3_000_000, "filed": "2026-08-12"}]}},
    "ClassOfWarrantOrRightExercisePriceOfWarrantsOrRights": {"units": {"USD/shares": [
        {"end": "2026-06-30", "val": 1.10, "filed": "2026-08-12"}]}},
}}}


def test_financials_runway_and_warrants():
    from pennybot.dilution import financials
    f = financials(FACTS, D)
    assert f["cash"] == 3_000_000 and f["cash_date"] == "2026-06-30"        # 미래 제출분 제외
    assert abs(f["burn_month"] - 1_000_000) < 20_000                        # 6개월 −600만$ → 월 ~100만$
    assert 0 <= f["runway_months"] < 0.5                                     # 6/30 이후 ~3.7개월 경과 → 거의 소진
    p = build_profile([], D, 20e6, f, price=1.20, shares=10e6)
    assert any("증자 임박" in r for r in p.reasons)
    assert any("워런트 발행주식의 30% (내가격)" in r for r in p.reasons)
    assert p.score >= 50
