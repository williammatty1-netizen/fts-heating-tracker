"""
Lightweight self-tests for fts_heating_tracker.py that run entirely offline
(no live calls to the Find a Tender API or to Telegram/Slack).
 
Run with:  python -m pytest tests/  (or just: python tests/test_tracker.py)
"""
 
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
 
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
 
import fts_heating_tracker as tr  # noqa: E402
 
 
def _release(
    ocid,
    title,
    description="",
    locality="Manchester",
    region="Greater Manchester",
    release_id="r1",
    buyer_name="Manchester City Council",
    postcode=None,
):
    address = {"locality": locality, "region": region}
    if postcode:
        address["postalCode"] = postcode
    return {
        "ocid": ocid,
        "id": release_id,
        "date": "2026-08-24T09:00:00Z",
        "tag": ["tender"],
        "buyer": {"id": "b1", "name": buyer_name},
        "parties": [
            {
                "id": "b1",
                "name": buyer_name,
                "roles": ["buyer"],
                "address": address,
            }
        ],
        "tender": {
            "title": title,
            "description": description,
            "value": {"amount": 250000, "currency": "GBP"},
            "tenderPeriod": {"endDate": "2026-09-30T12:00:00Z"},
            "documents": [{"url": f"https://www.find-tender.service.gov.uk/Notice/{ocid}"}],
        },
    }
 
 
def make_fake_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    resp.raise_for_status.side_effect = None
    return resp
 
 
def test_word_boundary_avoids_false_positive_mep():
    """'MEP' must not match inside an unrelated word like 'developments'."""
    patterns = tr._compile_keyword_patterns(["MEP"])
    _, pat = patterns[0]
    assert pat.search("MEP installation works") is not None
    assert pat.search("major developments planned") is None
 
 
def test_match_requires_both_location_and_heating():
    loc = tr._compile_keyword_patterns(tr.DEFAULT_LOCATION_KEYWORDS)
    heat = tr._compile_keyword_patterns(tr.DEFAULT_HEATING_KEYWORDS)
 
    good = _release("ocds-aaa-1", "Air source heat pump installation, Stockport leisure centre")
    assert tr.match_release(good, loc, heat) is not None
 
    wrong_location = _release(
        "ocds-aaa-2", "Air source heat pump installation, Leeds leisure centre",
        locality="Leeds", region="West Yorkshire", buyer_name="Leeds City Council",
    )
    assert tr.match_release(wrong_location, loc, heat) is None
 
    wrong_topic = _release("ocds-aaa-3", "Grounds maintenance contract, Salford parks")
    assert tr.match_release(wrong_topic, loc, heat) is None
 
 
def test_postcode_area_extraction_avoids_ml_vs_m_confusion():
    # "M" (Manchester) must not accidentally match "ML1 1AA" (Motherwell).
    assert tr._extract_postcode_area("M1 4WP") == "M"
    assert tr._extract_postcode_area("ML1 1AA") == "ML"
    assert tr._extract_postcode_area("SK3 0SD") == "SK"
    assert tr._extract_postcode_area("") is None
    assert tr._extract_postcode_area(None) is None
 
 
def test_postcode_area_matching_independent_of_free_text_keywords():
    """A buyer in Wigan (WN) with no 'Manchester/Trafford/Salford/Stockport'
    text anywhere should still match via the postcode-area list, and a
    postcode outside the target areas should not."""
    loc = tr._compile_keyword_patterns(tr.DEFAULT_LOCATION_KEYWORDS)
    heat = tr._compile_keyword_patterns(tr.DEFAULT_HEATING_KEYWORDS)
    areas = set(tr.DEFAULT_POSTCODE_AREAS)
 
    in_area = _release(
        "ocds-pc-1",
        "HIU installation, community centre refurbishment",
        locality="Wigan", region="North West England", buyer_name="Wigan Council",
        postcode="WN1 1AB",
    )
    hit = tr.match_release(in_area, loc, heat, postcode_areas=areas)
    assert hit is not None
    loc_hits, heat_hits = hit
    assert any("WN" in h for h in loc_hits)
 
    out_of_area = _release(
        "ocds-pc-2",
        "HIU installation, community centre refurbishment",
        locality="Motherwell", region="North Lanarkshire", buyer_name="North Lanarkshire Council",
        postcode="ML1 1AA",
    )
    assert tr.match_release(out_of_area, loc, heat, postcode_areas=areas) is None
 
    # Without any postcode_areas passed, behaviour is unchanged (back-compat).
    assert tr.match_release(in_area, loc, heat) is None
 
 
def test_dedupe_across_runs(tmp_path):
    state_path = tmp_path / "seen.json"
    state = tr.load_state(state_path)
 
    loc = tr._compile_keyword_patterns(tr.DEFAULT_LOCATION_KEYWORDS)
    heat = tr._compile_keyword_patterns(tr.DEFAULT_HEATING_KEYWORDS)
    release = _release("ocds-bbb-1", "District heat network extension, Trafford Park")
    hit = tr.match_release(release, loc, heat)
    assert hit is not None
    m = tr.build_match(release, *hit)
 
    first_pass = tr.dedupe_new_matches([m], state)
    assert len(first_pass) == 1
    tr.save_state(state_path, state)
 
    reloaded = tr.load_state(state_path)
    second_pass = tr.dedupe_new_matches([m], reloaded)
    assert len(second_pass) == 0, "already-seen ocid must not be reported again"
 
 
def test_pagination_follows_links_next():
    page1 = {
        "releases": [_release("ocds-p-1", "HIU replacement, Salford tower blocks", release_id="p1")],
        "links": {"next": f"{tr.API_BASE}?cursor=abc123"},
    }
    page2 = {
        "releases": [_release("ocds-p-2", "MVHR upgrade, Stockport apartments", release_id="p2")],
        "links": {},
    }
 
    session = MagicMock()
    session.get.side_effect = [make_fake_response(page1), make_fake_response(page2)]
 
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    releases = list(tr.fetch_releases(session, now - timedelta(hours=1), now))
 
    assert [r["ocid"] for r in releases] == ["ocds-p-1", "ocds-p-2"]
    assert session.get.call_count == 2
 
 
def test_end_to_end_dry_run(tmp_path, capsys):
    """Full main() pass against a mocked API, in dry-run mode (no network,
    no state mutation, no external notification calls)."""
    payload = {
        "releases": [
            _release("ocds-e2e-1", "Heat pump & MEP works, Manchester school", release_id="e1"),
            _release("ocds-e2e-2", "Road resurfacing, Manchester", release_id="e2"),  # no heating kw
        ],
        "links": {},
    }
    state_file = tmp_path / "state.json"
 
    with patch.object(tr, "build_session") as build_session_mock:
        session = MagicMock()
        session.get.return_value = make_fake_response(payload)
        build_session_mock.return_value = session
 
        rc = tr.main(["--state-file", str(state_file), "--dry-run"])
 
    assert rc == 0
    assert not state_file.exists(), "dry-run must not write state"
 
 
if __name__ == "__main__":
    # Allow `python tests/test_tracker.py` without pytest installed.
    import types
 
    mod_tests = [
        (name, fn)
        for name, fn in list(globals().items())
        if name.startswith("test_") and isinstance(fn, types.FunctionType)
    ]
    failures = 0
    for name, fn in mod_tests:
        try:
            argcount = fn.__code__.co_argcount
            if argcount == 0:
                fn()
            else:
                # Minimal manual fixtures for tmp_path/capsys when run standalone
                import tempfile
 
                args = []
                for pname in fn.__code__.co_varnames[:argcount]:
                    if pname == "tmp_path":
                        args.append(Path(tempfile.mkdtemp()))
                    elif pname == "capsys":
                        args.append(None)
                fn(*args)
            print(f"PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
    sys.exit(1 if failures else 0)
