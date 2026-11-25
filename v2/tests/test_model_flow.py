import json
import sqlite3

import pytest

from pennybot2.core.config import Config
from pennybot2.core.store import Store
from pennybot2.scoring import model as M
from pennybot2.workers import daily
from pennybot2.workers.surge import enrich
from tests.test_model import walk, with_planted


def fill_daily(store, n_sym=60, planted=True):
    rows = []
    for i in range(n_sym):
        for r in (with_planted(i) if planted else [tuple(x) for x in walk(i)]):
            rows.append((f"S{i}", *r))
    store.put_daily(rows)


def test_old_db_without_prob_column_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE candidates (asof TEXT, symbol TEXT, score INTEGER, why TEXT, minus TEXT, pc REAL, PRIMARY KEY (asof, symbol))")
    db.commit(); db.close()
    s = Store(path)                                           # 열면서 prob 컬럼을 추가
    s.put_candidates("2026-11-25", [{"s": "A", "score": 7, "why": ["x"], "minus": [], "pc": 1.0, "prob": 0.031}])
    assert s.db.execute("SELECT prob FROM candidates").fetchone()[0] == 0.031
    s.put_candidates("2026-11-25", [{"s": "B", "score": 5, "why": [], "minus": [], "pc": 1.0}])      # prob 없는 규칙 점수 후보도 저장됨
    assert s.db.execute("SELECT prob FROM candidates WHERE symbol='B'").fetchone()[0] is None


def test_build_candidates_model_ranks_by_probability():
    s = Store(":memory:")
    fill_daily(s, 30)
    ds = M.build_dataset({f"S{i}": s.daily_rows(f"S{i}") for i in range(30)}, with_rule=False)
    mdl = M.train(ds)
    asof, items = daily.build_candidates_model(s, [f"S{i}" for i in range(30)], mdl, top=10)
    assert asof and len(items) == 10
    assert [i["prob"] for i in items] == sorted((i["prob"] for i in items), reverse=True)
    assert all(0 < i["prob"] < 1 and i["score"] == int(round(i["prob"] * 100)) for i in items)


def test_only_adopted_model_is_loaded(tmp_path):
    cfg = Config(state_dir=tmp_path)
    assert daily.load_adopted_model(cfg) is None                       # 파일 없음
    M.save({"metrics": {"adopt": False}}, tmp_path / "model.json")
    assert daily.load_adopted_model(cfg) is None                       # 채택 안 된 모델은 안 씀
    M.save({"metrics": {"adopt": True}}, tmp_path / "model.json")
    assert daily.load_adopted_model(cfg) == {"metrics": {"adopt": True}}


def test_alert_text_uses_probability_only_when_model_candidate():
    sig = {"s": "A", "ts": 0, "kind": "surge", "value": 12.0, "text": "A +12%"}
    m = enrich(sig, {"s": "A", "score": 4, "prob": 0.043, "why": ["전날 거래대금 배수", "낮은 가격"], "minus": []})
    assert "급등 확률 약 4.3%" in m["text"] and "전날 거래대금 배수" in m["text"]
    r = enrich(sig, {"s": "A", "score": 6, "why": ["a"], "minus": []})          # 옛 규칙 점수 후보
    assert "확률은 재검증 중" in r["text"] and "%" not in r["text"].split("★")[1]


def test_train_cli_saves_model_only_if_adopted(tmp_path, capsys):
    s = Store(tmp_path / "m.db")
    fill_daily(s, 150)
    s.close()
    out = tmp_path / "state" / "model.json"
    assert M.main([str(tmp_path / "m.db"), f"--out={out}"]) == 0
    text = capsys.readouterr().out
    assert "학습된 요인" in text and "교정" in text
    assert (tmp_path / "state" / "model_candidate.json").exists()      # 참고용은 항상 저장
    adopted = json.loads((tmp_path / "state" / "model_candidate.json").read_text(encoding="utf-8"))["metrics"]["adopt"]
    assert out.exists() == adopted                                      # 채택 여부와 model.json 존재가 일치
    assert ("채택:" in text) == adopted


def test_train_cli_needs_a_db(capsys):
    assert M.main([]) == 2
    assert "로지스틱 회귀" in capsys.readouterr().out                  # 사용법(모듈 설명)을 보여 준다
    assert M.main(["/없는/경로.db"]) == 2
