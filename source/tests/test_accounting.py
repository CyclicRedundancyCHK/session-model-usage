from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from session_model_usage.accounting import Rollout, Usage, UsageService
from session_model_usage.cdp import Observation, anchor_physical, choose_observation, bind_window, local_url, details_rectangle, screen_matches


ROOT = "00000000-0000-0000-0000-000000000001"
CHILD = "00000000-0000-0000-0000-000000000002"
GRANDCHILD = "00000000-0000-0000-0000-000000000003"


def row(kind, payload, second=1):
    return {"timestamp": f"2026-09-29T00:00:{second:02d}Z", "type": kind, "payload": payload}


def usage(inp=100, out=10, cached=50, reason=4):
    return asdict(Usage(inp, cached, 0, out, reason, inp + out))


def meta(owner=ROOT, second=0, fork=None):
    return row("session_meta", {"id": owner, "forked_from_id": fork}, second)


def context(model="gpt-6-sol", turn="turn-1", second=1, effort=None):
    return row("turn_context", {"model": model, "turn_id": turn, **({"effort": effort} if effort is not None else {})}, second)


def event(last=None, cumulative=None, second=2):
    last = last or usage()
    return row("event_msg", {"type": "token_count", "info": {
        "last_token_usage": last, "total_token_usage": cumulative or last}}, second)


def record(owner=ROOT, response="response-1", value=None, cumulative=None, turn="turn-1", second=2):
    value = value or usage()
    return row("token_usage_record", {"thread_id": owner, "response_id": response,
        "turn_id": turn, "usage": value, "thread_token_usage": cumulative or value}, second)


def totals(parser):
    entries, warnings = parser.entries()
    total = Usage()
    for entry in entries:
        total += entry.usage
    return entries, total, warnings


class ParserTests(unittest.TestCase):
    def parser(self, rows, owner=ROOT):
        parser = Rollout(Path("unused.jsonl"), owner)
        for r in rows:
            parser.feed(r)
        return parser

    def test_modern_record_and_mirror_count_once(self):
        parser = self.parser([meta(), context(), event(), record(), event(second=3), record()])
        entries, total, warnings = totals(parser)
        self.assertEqual(total, Usage.parse(usage()))
        self.assertEqual(len(entries), 1)
        self.assertFalse(warnings)
        self.assertEqual(total.total_tokens, total.input_tokens + total.output_tokens)

    def test_model_switch_and_explicit_reroute(self):
        parser = self.parser([meta(), context(), record(),
            context("gpt-6-astra", "turn-2", 3),
            row("event_msg", {"type": "model_rerouted", "to_model": "gpt-6-luna"}, 4),
            record(response="r2", turn="turn-2", cumulative=usage(200, 20, 100, 8), second=5)])
        entries, total, warnings = totals(parser)
        self.assertEqual([x.model for x in entries], ["gpt-6-sol", "gpt-6-luna"])
        self.assertEqual(total.total_tokens, 220)
        self.assertFalse(warnings)

    def test_effort_switch_and_late_response_keep_request_attribution(self):
        parser = self.parser([meta(), context(effort="max"), record(),
            context(turn="turn-2", second=3, effort="high"),
            record(response="r2", turn="turn-2", second=4),
            record(response="late", turn="turn-1", second=5), record()])
        entries = totals(parser)[0]
        self.assertEqual([e.reasoning_effort for e in entries], ["max", "high", "max"])
        self.assertEqual(totals(parser)[1].total_tokens, 330)

    def test_missing_effort_does_not_inherit_another_turn(self):
        parser = self.parser([meta(), context(effort="max"), record(),
            row("event_msg", {"type": "task_started", "turn_id": "turn-2"}, 3),
            record(response="r2", turn="turn-2", second=4),
            context(turn="turn-2", second=5), record(response="r3", turn="turn-2", second=6)])
        self.assertEqual([e.reasoning_effort for e in totals(parser)[0]], ["max", None, None])

    def test_explicit_effort_none_settings_and_reroute(self):
        explicit = record(response="explicit", second=7)
        explicit["payload"]["reasoning_effort"] = "none"
        parser = self.parser([meta(), context(effort="max"), record(),
            row("event_msg", {"type": "thread_settings_applied", "thread_settings": {
                "model": "gpt-6-sol", "reasoning_effort": "high"}}, 3),
            record(response="r2", second=4),
            row("event_msg", {"type": "model_rerouted", "to_model": "gpt-6-luna", "reasoning_effort": "low"}, 5),
            record(response="r3", second=6), explicit])
        entries = totals(parser)[0]
        self.assertEqual([e.reasoning_effort for e in entries], ["max", "high", "low", "none"])
        self.assertEqual(entries[2].model, "gpt-6-luna")

    def test_legacy_effort_and_unattributed_gap(self):
        parser = self.parser([meta(), context(effort="high"), event(),
            context(turn="turn-2", second=3, effort="max"),
            event(cumulative=usage(300,30,150,12), second=4)])
        entries = totals(parser)[0]
        self.assertEqual([e.reasoning_effort for e in entries], ["high", None])
        self.assertIsNone(entries[1].model)

    def test_fork_does_not_copy_parent_effort(self):
        parser = self.parser([meta(CHILD, 10, ROOT), context(effort="max"),
            record(ROOT), context(turn="child-turn", second=11, effort="low"),
            record(CHILD, turn="child-turn", second=12)], CHILD)
        entries = totals(parser)[0]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].reasoning_effort, "low")

    def test_legacy_duplicate_rate_limit_event(self):
        parser = self.parser([meta(), context(), event(), event(second=3),
            row("event_msg", {"type": "token_count", "info": None}, 4),
            event(cumulative=usage(200, 20, 100, 8), second=5)])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 220)
        self.assertEqual(len(entries), 2)
        self.assertFalse(warnings)

    def test_upgrade_from_legacy_to_modern(self):
        parser = self.parser([meta(), context(), event(),
            event(cumulative=usage(200,20,100,8), second=3),
            record(response="r2", cumulative=usage(200,20,100,8), second=3)])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 220)
        self.assertEqual(len(entries), 2)
        self.assertFalse(warnings)

    def test_divergent_counters_after_compaction_do_not_double_count(self):
        parser = self.parser([meta(), context(), record(), event(),
            record(response="r2", cumulative=usage(200,20,100,8), second=3),
            event(cumulative=usage(130,13,60,5),second=4),
            record(response="r3", cumulative=usage(300,30,150,12), second=5),
            event(cumulative=usage(230,23,110,9),second=6),
            event(cumulative=usage(230,23,110,9),second=7)])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 330)
        self.assertEqual(len(entries), 3)
        self.assertFalse(warnings)

    def test_context_display_reset_is_not_a_request(self):
        display = usage(0,0,0,0);display["total_tokens"]=24
        parser = self.parser([meta(),context(),record(),event(),
            event(last=display,cumulative=usage(),second=3),
            event(last=display,cumulative=usage(),second=4)])
        self.assertEqual(totals(parser)[1].total_tokens,110)
        self.assertFalse(totals(parser)[2])

    def test_repeated_notifications_before_primary_count_once(self):
        parser=self.parser([meta(),context(),event(),event(second=3),record(second=3)])
        self.assertEqual(totals(parser)[1].total_tokens,110)
        self.assertEqual(len(totals(parser)[0]),1)

    def test_legacy_reset_repeats_an_earlier_fingerprint(self):
        parser=self.parser([meta(),context(),event(),
            event(cumulative=usage(200,20,100,8),second=3),
            context(turn="turn-2",second=4),event(second=5)])
        self.assertEqual(totals(parser)[1].total_tokens,330)
        self.assertTrue(any("重置" in w for w in totals(parser)[2]))

    def test_carried_old_counter_at_new_turn_does_not_consume_future_record(self):
        parser=self.parser([meta(),context(),event(),record(),
            context(turn="turn-2",second=3),event(second=4),
            record(response="r2",turn="turn-2",cumulative=usage(200,20,100,8),second=5),
            event(cumulative=usage(200,20,100,8),second=5)])
        self.assertEqual(totals(parser)[1].total_tokens,220)
        self.assertEqual(len(totals(parser)[0]),2)

    def test_identical_legacy_request_after_modern_is_not_a_mirror(self):
        parser = self.parser([meta(),context(),record(),event(),
            event(cumulative=usage(200,20,100,8),second=3)])
        entries,total,warnings=totals(parser)
        self.assertEqual(total.total_tokens,220)
        self.assertEqual(len(entries),2)
        self.assertFalse(warnings)

    def test_missing_request_inside_modern_interval_is_unattributed(self):
        parser = self.parser([meta(),context(),record(),event(),
            record(response="r2",cumulative=usage(300,30,150,12),second=3),
            event(cumulative=usage(300,30,150,12),second=3)])
        entries,total,warnings=totals(parser)
        self.assertEqual(total.total_tokens,330)
        self.assertEqual(len(entries),3)
        self.assertIsNone(entries[-1].model)
        self.assertTrue(warnings)

    def test_task_started_and_nested_settings_assign_current_turn(self):
        parser=self.parser([meta(),context(),record(),
            row("event_msg",{"type":"task_started","turn_id":"turn-2"},3),
            row("event_msg",{"type":"thread_settings_applied","thread_settings":{"model":"gpt-6-astra"}},3),
            record(response="r2",turn="turn-2",second=4)])
        self.assertEqual([e.model for e in totals(parser)[0]],["gpt-6-sol","gpt-6-astra"])

    def test_fork_does_not_count_copied_parent_history(self):
        parser = self.parser([meta(CHILD, 10, ROOT), meta(ROOT), context(), event(), record(),
            context("gpt-6-astra", "child-turn", 11),
            event(second=12), record(CHILD,"child-r1",turn="child-turn",second=12)], CHILD)
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 110)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].model, "gpt-6-astra")
        self.assertFalse(warnings)

    def test_legacy_fork_inherited_counter_is_excluded(self):
        parser = self.parser([meta(CHILD, 10, ROOT), context(second=11),
            event(cumulative=usage(1100,110,550,44),second=12)], CHILD)
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 110)
        self.assertTrue(warnings)

    def test_legacy_counter_reset_uses_last_only(self):
        parser = self.parser([meta(), context(), event(),
            event(last=usage(30,3,10,1), cumulative=usage(30,3,10,1), second=3)])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 143)
        self.assertTrue(any("重置" in w for w in warnings))

    def test_unknown_model_is_not_guessed_from_current_selection(self):
        parser = self.parser([meta(), record()])
        entries, total, warnings = totals(parser)
        self.assertIsNone(entries[0].model)
        self.assertTrue(any("模型归属" in w for w in warnings))

    def test_legacy_gap_is_unattributed(self):
        parser = self.parser([meta(), context(), event(cumulative=usage(300,30,150,12))])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 330)
        self.assertIsNone(entries[0].model)
        self.assertTrue(warnings)

    def test_bad_record_is_partial_not_zero_request(self):
        bad = record(); del bad["payload"]["usage"]["total_tokens"]
        parser = self.parser([meta(), context(), bad])
        entries, total, warnings = totals(parser)
        self.assertEqual(entries, [])
        self.assertTrue(warnings)

    def test_incremental_partial_line_restart_and_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rollout.jsonl"
            first = "\n".join(json.dumps(x) for x in [meta(),context(),event()])+"\n"
            extra = json.dumps(record()).encode()
            path.write_bytes(first.encode()+extra[:35])
            parser = Rollout(path,ROOT); parser.refresh()
            self.assertEqual(totals(parser)[1].total_tokens,110)
            with path.open("ab") as handle:handle.write(extra[35:]+b"\n")
            parser.refresh(); self.assertEqual(totals(parser)[1].total_tokens,110)
            parser.refresh(); self.assertEqual(totals(parser)[1].total_tokens,110)
            restored=Rollout(path,ROOT); restored.refresh()
            self.assertEqual(totals(parser)[1],totals(restored)[1])
            path.write_text(json.dumps(meta())+"\n",encoding="utf-8")
            parser.refresh(); self.assertEqual(totals(parser)[1].total_tokens,0)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.home=Path(self.temporary.name)
        self.con=sqlite3.connect(self.home/"state_5.sqlite")
        self.con.executescript("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,agent_path TEXT,source TEXT,model TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);")

    def tearDown(self):
        self.con.close();self.temporary.cleanup()

    def add(self, owner, parent=None, archived=False, model="gpt-6-sol", effort=None):
        path=self.home/f"rollout-{owner}.jsonl"
        rows=[meta(owner,10 if parent else 0,parent),context(model,"own-turn",11 if parent else 1,effort),
              event(second=12 if parent else 2),record(owner,owner,turn="own-turn",second=12 if parent else 2)]
        path.write_text("\n".join(json.dumps(x) for x in rows)+"\n",encoding="utf-8")
        self.con.execute("INSERT INTO threads VALUES(?,?,?,?,?)",(owner,str(path),None,"vscode",model))
        if parent:self.con.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)",(parent,owner,"closed" if archived else "open"))
        self.con.commit()

    def test_recursive_closed_children_cycle_and_conservation(self):
        self.add(ROOT);self.add(CHILD,ROOT,True,"gpt-6-astra");self.add(GRANDCHILD,CHILD)
        self.con.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)",(GRANDCHILD,ROOT,"open"));self.con.commit()
        service=UsageService(self.home)
        snapshot=service.query(ROOT)
        self.assertEqual(snapshot["status"],"complete")
        self.assertEqual(snapshot["totals"]["total_tokens"],330)
        self.assertEqual(len(snapshot["threads"]),3)
        self.assertEqual(sum(m["totals"]["total_tokens"] for m in snapshot["models"]),330)
        self.assertEqual(sum(t["totals"]["total_tokens"] for t in snapshot["threads"]),330)
        self.assertEqual(service.query(ROOT,False)["totals"]["total_tokens"],110)

    def test_missing_child_file_is_partial(self):
        self.add(ROOT);self.add(CHILD,ROOT)
        (self.home/f"rollout-{CHILD}.jsonl").unlink()
        snapshot=UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot["status"],"partial")
        self.assertEqual(snapshot["totals"]["total_tokens"],110)
        self.assertIsNone(snapshot["threads"][1]["totals"])

    def test_model_effort_groups_conserve_all_fields_and_sources(self):
        self.add(ROOT, effort="max"); self.add(CHILD, ROOT, effort="high"); self.add(GRANDCHILD, CHILD)
        snapshot = UsageService(self.home).query(ROOT)
        model = snapshot["models"][0]
        groups = {g["reasoning_effort"]: g for g in model["reasoning_efforts"]}
        self.assertEqual(set(groups), {"max", "high", None})
        self.assertEqual(groups["max"]["main"]["total_tokens"], 110)
        self.assertEqual(groups["high"]["subagents"]["total_tokens"], 110)
        self.assertEqual(groups[None]["subagents"]["total_tokens"], 110)
        for key in model["totals"]:
            self.assertEqual(sum(g["totals"][key] for g in groups.values()), model["totals"][key])
            self.assertEqual(sum(g["main"][key] for g in groups.values()), model["main"][key])
            self.assertEqual(sum(g["subagents"][key] for g in groups.values()), model["subagents"][key])
        self.assertEqual(snapshot["status"], "complete")

    def test_empty_session_is_pending_not_zero(self):
        path=self.home/"empty.jsonl";path.write_text(json.dumps(meta())+"\n")
        self.con.execute("INSERT INTO threads VALUES(?,?,?,?,?)",(ROOT,str(path),None,"vscode",None));self.con.commit()
        snapshot=UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot["status"],"pending")
        self.assertIsNone(snapshot["totals"])

    def test_older_index_without_relationships_is_partial(self):
        self.add(ROOT)
        self.con.execute("DROP TABLE thread_spawn_edges")
        self.con.execute("ALTER TABLE threads DROP COLUMN source")
        self.con.commit()
        snapshot=UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'],110)
        self.assertEqual(snapshot['status'],'partial')
        self.assertTrue(any('父子关系' in w for w in snapshot['warnings']))


class FollowTests(unittest.TestCase):
    def observation(self, ident, focused=True, minimized=False):
        return Observation(ident,{"threadId":ROOT,"visible":True,"composer":{"top":700},"focused":focused},
                           {"windowState":"minimized" if minimized else "normal"})

    def binding_pair(self, ident="a", hwnd=101, x=0, focused=False, visible=True):
        view = self.observation(ident, focused)
        view.data.update(visible=visible, screen={"x":x,"y":0,"width":1000,"height":800,"workX":0,"workY":0})
        window = {"hwnd":hwnd,"rectangles":[(x,0,1000,800)],"scale":1,"work_origin":(0,0)}
        return view,window

    def test_background_window_keeps_verified_attachment(self):
        view,window = self.binding_pair()
        self.assertEqual(bind_window([view],[window],999,"a",101),(view,window))

    def test_single_background_window_can_bind_on_launch(self):
        view,window = self.binding_pair()
        self.assertEqual(bind_window([view],[window],999),(view,window))

    def test_occluded_cdp_page_keeps_visible_native_attachment(self):
        view,window = self.binding_pair(visible=False)
        self.assertEqual(bind_window([view],[window],999,"a",101),(view,window))

    def test_active_window_switch_overrides_previous_background_binding(self):
        a,wa = self.binding_pair()
        b,wb = self.binding_pair("b",202,1200,focused=True)
        self.assertEqual(bind_window([a,b],[wa,wb],202,"a",101),(b,wb))
        b.data["focused"] = False
        self.assertEqual(bind_window([a,b],[wa,wb],999,"b",202),(b,wb))

    def test_ambiguous_background_windows_do_not_guess(self):
        a,wa = self.binding_pair()
        b,wb = self.binding_pair("b",202)
        self.assertIsNone(bind_window([a,b],[wa,wb],999))
        self.assertEqual(bind_window([a,b],[wa,wb],999,"b",202),(b,wb))

    def test_missing_minimized_or_replaced_window_does_not_reuse_binding(self):
        view,window = self.binding_pair()
        self.assertIsNone(bind_window([view],[],999,"a",101))
        view.bounds["windowState"] = "minimized"
        self.assertIsNone(bind_window([view],[window],999,"a",101))
        view.bounds["windowState"] = "normal"
        window["rectangles"] = [(1200,0,1000,800)]
        self.assertIsNone(bind_window([view],[window],999,"a",101))

    def test_hidden_host_does_not_jump_to_matching_auxiliary_window(self):
        view, auxiliary = self.binding_pair(hwnd=202)
        self.assertIsNone(bind_window([view],[auxiliary],999,"a",101))
        view.data["focused"] = True
        self.assertEqual(bind_window([view],[auxiliary],202,"a",101),(view,auxiliary))

    def test_same_named_windows_use_focused_target_not_recent_activity(self):
        a,b=self.observation("target-a"),self.observation("target-b",False)
        self.assertEqual(choose_observation([b,a]).target_id,"target-a")
        a.data["focused"]=False
        self.assertIsNone(choose_observation([a,b]))
        self.assertEqual(choose_observation([a,b],"target-b",True).target_id,"target-b")

    def test_minimized_or_missing_composer_hides(self):
        minimized=self.observation("a",True,True)
        self.assertIsNone(choose_observation([minimized]))
        missing=self.observation("b");missing.data["composer"]=None
        self.assertIsNone(choose_observation([missing]))

    def test_dpi_zoom_and_negative_monitor_coordinates(self):
        data={"composer":{"right":900,"top":650},"viewport":{"width":1000,"height":800},
              "toolbarGap":{"left":200,"top":750,"width":500,"height":30}}
        self.assertEqual(anchor_physical(data,(-1500,100),(1500,1200),(270,45)),(-960,1225))
        self.assertEqual(anchor_physical(data,(100,100),(1000,830),(180,30)),(460,880))

    def test_missing_or_narrow_gap_never_overlaps_controls(self):
        data={"composer":{"right":900,"top":650},"viewport":{"width":1000,"height":800}}
        self.assertIsNone(anchor_physical(data,(0,0),(1000,800),(180,28)))
        data["toolbarGap"]={"left":300,"top":750,"width":90,"height":28}
        self.assertIsNone(anchor_physical(data,(0,0),(1000,800),(180,28)))
        data["toolbarGap"]["width"]=220
        self.assertEqual(anchor_physical(data,(0,0),(1000,800),(180,28)),(320,750))

    def test_connection_rejects_external_hosts_and_wrong_port(self):
        self.assertEqual(local_url("ws://127.0.0.1:1234/devtools/page/x",1234,True),"ws://127.0.0.1:1234/devtools/page/x")
        for bad in ("ws://example.com:1234/x","ws://127.0.0.1:1235/x","wss://127.0.0.1:1234/x"):
            with self.assertRaises(RuntimeError):local_url(bad,1234,True)

    def test_details_stay_above_composer_and_fit_negative_monitor(self):
        data={"client_origin":(-1500,100),"client_size":(1200,900),"composer":{"right":1000,"top":550},"viewport":{"width":1000,"height":750}}
        rectangle=details_rectangle(data,(-500,650),1.5)
        x,y,width,height=rectangle
        self.assertGreaterEqual(x,-1500)
        self.assertGreaterEqual(y,100)
        self.assertLessEqual(x+width,-300)
        self.assertLess(y+height,760)
        data["composer"]["top"]=100
        self.assertIsNone(details_rectangle(data,(-500,200),1.5))

    def test_helper_browser_window_in_same_process_is_rejected(self):
        data={"screen":{"x":320,"y":106,"width":1280,"height":820,"workX":0,"workY":0}}
        self.assertTrue(screen_matches(data,(320,106,1280,820),1,(0,0)))
        self.assertTrue(screen_matches(data,[(26,26,1440,760),(320,106,1280,820)],1,(0,0)))
        self.assertFalse(screen_matches(data,(26,26,1440,760),1,(0,0)))
        self.assertFalse(screen_matches(data,[(26,26,1440,760),(600,350,600,300)],1,(0,0)))

    def test_maximized_client_viewport_matches_even_if_native_frame_differs(self):
        data={"screen":{"x":0,"y":0,"width":1920,"height":1032,"workX":0,"workY":0}}
        frame=(-8,-8,1936,1048)
        client=(0,0,1920,1032)
        self.assertTrue(screen_matches(data,[frame,client],1,(0,0)))
        self.assertFalse(screen_matches(data,frame,1,(0,0)))

    def test_window_matching_uses_monitor_origin_for_mixed_dpi(self):
        data={"screen":{"x":-1000,"y":100,"width":800,"height":600,"workX":-1280,"workY":0}}
        self.assertTrue(screen_matches(data,(-1500,150,1200,900),1.5,(-1920,0)))
        self.assertFalse(screen_matches(data,(-1000,100,800,600),1.5,(-1920,0)))


if __name__ == "__main__":
    unittest.main()
