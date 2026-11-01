#!/usr/bin/env python3
"""Run with python3 test_opener.py; no dotfiles or live Rex needed."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

PACKAGE = Path(__file__).resolve().parent


class StandaloneOpener(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rex opener's ")
        self.addCleanup(self.temporary.cleanup)
        self.package = Path(self.temporary.name) / "plugin"
        shutil.copytree(PACKAGE, self.package, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        helper = self.package / "opener.py"
        self.assertTrue(helper.is_file(), "The exported plugin must contain its own opener")
        spec = importlib.util.spec_from_file_location("rex_opener", helper)
        assert spec and spec.loader
        self.opener = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.opener)

    def test_exported_package_extracts_urls_and_paths_without_dotfiles(self):
        target = self.package / "some file.py"
        target.touch()
        rows = self.opener.text_targets("example.com/help).\n[file](some file.py:17:2)\n", self.package.resolve())
        self.assertIn(("url", "https://example.com/help", "example.com/help", ""), rows)
        self.assertIn(("text", str(target.resolve()), "some file.py:17", "17"), rows)

    def test_capture_uses_utf8_byte_offsets(self):
        rex = Mock()
        rex.call.side_effect = [{"content": "é", "id": 12, "eof": False}, {"data": " next", "eof": True}]
        self.assertEqual(self.opener.capture_text(rex, "s", "b"), "é next")
        self.assertEqual(rex.call.call_args.kwargs["args"]["offset"], 2)

    def test_url_permutations_preserve_the_complete_target(self):
        cases = {
            "https://example.com/a?q=x&y=[1]": ["https://example.com/a?q=x&y=[1]"],
            "https://example.com/'single'": ["https://example.com/'single'"],
            "(https://example.com/a_(b)).": ["https://example.com/a_(b)"],
            "[docs](https://docs.rs/serde/latest/)": ["https://docs.rs/serde/latest/"],
            "https://a.com/x,https://b.com/y": ["https://b.com/y", "https://a.com/x"],
            "foo.ai baz.xyz qux.tech x.gg x.so x.cloud": [
                "https://" + x for x in ("x.cloud", "x.so", "x.gg", "qux.tech", "baz.xyz", "foo.ai")
            ],
            "ftp://example.com/x": ["ftp://example.com/x"],
            "a@example.com missing.py missing.rs missing.md missing.sh": [],
            "HTTP://localhost:3000/a%20b#frag": ["HTTP://localhost:3000/a%20b#frag"],
            "<https://example.com/x>": ["https://example.com/x"],
            "'https://example.com/x'": ["https://example.com/x"],
            '"https://example.com/x"': ["https://example.com/x"],
            "`https://example.com/x`": ["https://example.com/x"],
            "http://127.0.0.1:8080/x?y=1": ["http://127.0.0.1:8080/x?y=1"],
            "https://[::1]:8080/x": ["https://[::1]:8080/x"],
            "https://user:pw@host.example.com/p": ["https://user:pw@host.example.com/p"],
            "https://xn--bcher-kva.example/": ["https://xn--bcher-kva.example/"],
            "www.example.com/help": ["https://www.example.com/help"],
            "ssh://example.com/a": [],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual([row[1] for row in self.opener.text_targets(text, self.package)], expected)

    def test_existing_file_url_quoted_space_path_and_directory(self):
        target = self.package / "space file.py"
        target.touch()
        folder = self.package / "folder"
        folder.mkdir()
        for text, kind, path, line in (
            (target.as_uri(), "text", target, ""),
            ('"space file.py:12:3"', "text", target, "12"),
            ("*** Update File: space file.py", "text", target, ""),
            ("space file.py:12:3", "text", target, "12"),
            ("folder/", "dir", folder, ""),
        ):
            with self.subTest(text=text):
                rows = self.opener.text_targets(text, self.package)
                self.assertEqual([(row[0], row[1], row[3]) for row in rows], [(kind, str(path.resolve()), line)])

    def test_oversized_non_path_does_not_hide_later_urls(self):
        rows = self.opener.text_targets("x" * 4096 + "\nhttps://example.com", self.package)
        self.assertEqual([row[1] for row in rows], ["https://example.com"])

    def test_latest_occurrence_wins_across_urls_and_files(self):
        target = self.package / "file.py"
        target.touch()
        rows = self.opener.text_targets("https://a.com\nfile.py:17:2\nhttps://b.com\nhttps://a.com", self.package)
        self.assertEqual([row[1] for row in rows], ["https://a.com", "https://b.com", str(target.resolve())])

    def test_capture_rejects_a_stalled_blob(self):
        rex = Mock()
        rex.call.side_effect = [{"content": "é", "id": 12, "eof": False}, {"data": "", "eof": False}]
        with self.assertRaisesRegex(RuntimeError, "no progress"):
            self.opener.capture_text(rex, "s", "b")

    def test_text_path_and_line_are_quoted_for_editor(self):
        with patch.object(self.opener, "split_command") as split:
            self.opener.open_target(
                Mock(), "s", "b", "c", self.package, ("text", "/tmp/a file's.py", "source:17", "17")
            )
        self.assertEqual(self.opener.shlex.split(split.call_args.args[-1]), ["nvim", "+17", "--", "/tmp/a file's.py"])

    def test_cancelled_selection_does_not_open_anything(self):
        with (
            patch.object(self.opener, "capture_text", return_value=""),
            patch.object(self.opener, "text_targets", return_value=[("url", "https://a.com", "a", "")] * 2),
            patch.object(self.opener, "choose", return_value=""),
            patch.object(self.opener, "open_target") as opened,
        ):
            self.opener.open_text_picker(Mock(), "s", "b", "c", self.package)
        opened.assert_not_called()

    def test_missing_picker_is_reported_instead_of_treated_as_cancellation(self):
        with patch.object(self.opener, "popup", return_value=127):
            with self.assertRaisesRegex(RuntimeError, "fzf"):
                self.opener.choose(Mock(), "s", "b", "c", self.package, ["0\turl\texample.com"], "open ")

    def test_remote_terminal_is_rejected_before_reading_output(self):
        rex = Mock(identity={"host_id": "local"})
        rex.call.return_value = {"block": {"host_id": "remote"}}
        with self.assertRaisesRegex(RuntimeError, "not on this machine"):
            self.opener.context(rex, "s", "b", "c")
        self.assertEqual(rex.call.call_count, 1)

    def test_another_users_terminal_is_rejected(self):
        rex = Mock(identity={"host_id": "local"})
        rex.call.side_effect = [
            {"block": {"host_id": "local"}},
            {"child": {"user_id": self.opener.os.getuid() + 1, "path": "/bin/fish"}},
        ]
        with self.assertRaisesRegex(RuntimeError, "not owned"):
            self.opener.context(rex, "s", "b", "c")

    def test_editor_split_does_not_redirect_another_app(self):
        rex = Mock()
        rex.call.side_effect = [
            {"columns": 120, "rows": 40},
            {"block_ids": ["new"]},
            {"block": {"window_id": "w"}},
            {"clients": [{"client_id": "peer", "info": {"kind": "app"}, "session_ids": ["s"]}]},
        ]
        with patch.object(self.opener, "layout", return_value={}):
            route = self.opener.split_command(rex, "s", "b", "c", self.package, "nvim file")
        self.assertEqual(route, {"session": "s", "window": "w", "block": "new"})
        self.assertFalse(rex.call.call_args_list[1].kwargs["focus"])
        rex.action.assert_not_called()

    def test_picker_cleans_up_its_layer_even_when_select_fails(self):
        rex = Mock()
        rex.call.side_effect = [
            {"block": {"window_id": "w"}},
            {"windows": [{"window_id": "w"}]},
            {"layer_id": "picker-layer", "block_ids": ["picker"]},
            {},
        ]
        rex.action.side_effect = RuntimeError("client closed")
        with (
            patch.object(self.opener, "layout", return_value={"block": {"options": {}}}),
            patch.object(self.opener, "foreground_program", return_value=(123, "fish")),
        ):
            with self.assertRaisesRegex(RuntimeError, "client closed"):
                self.opener.popup(rex, "s", "b", "c", self.package, "fzf")
        rex.call.assert_called_with("session.close_layer", session_id="s", layer_id="picker-layer")
        self.assertFalse(
            any(
                call.args[0] in ("session.create", "session.new_window", "session.destroy")
                for call in rex.call.call_args_list
            )
        )

    def test_manual_close_is_cancellation_and_restores_zoom_only_in_source_tab(self):
        for moved in (False, True):
            with self.subTest(moved=moved):
                source = {
                    "window_id": "w",
                    "focused_block_id": "b",
                    "zoomed_block_id": "b",
                    "layers": [{"blocks": [{"block_id": "b"}]}],
                }
                rex = Mock()

                def call(method, **args):
                    if method == "session.describe_block":
                        return {"block": {"window_id": "w"}}
                    if method == "session.view":
                        return {"active_window_id": "other" if moved else "w", "windows": [source]}
                    if method == "session.new_layer":
                        return {"layer_id": "picker-layer", "block_ids": ["picker"]}
                    if method in ("com.superlogical.terminal.process", "session.close_layer"):
                        raise RuntimeError("not found")
                    return {}

                rex.call.side_effect = call
                with patch.object(self.opener, "foreground_program", return_value=(123, "fish")):
                    self.assertEqual(self.opener.popup(rex, "s", "b", "c", self.package, "fzf"), 130)
                self.assertEqual(any(c.args[0] == "session.zoom_block" for c in rex.call.call_args_list), not moved)

    def test_another_app_attachment_does_not_move_the_picker_out_of_neovim(self):
        rex = Mock()
        rex.call.return_value = {
            "block": {"window_id": "w"},
            "clients": [{"client_id": "peer", "info": {"kind": "app"}, "session_ids": ["s"]}],
        }
        target = {"socket": "/tmp/owned.sock"}
        with (
            patch.object(self.opener, "foreground_program", return_value=(123, "nvim")),
            patch.object(self.opener, "neovim_target", return_value=target),
            patch.object(self.opener, "popup_neovim", return_value=130) as popup,
        ):
            self.assertEqual(self.opener.popup(rex, "s", "b", "c", self.package, "fzf"), 130)
        popup.assert_called_once_with(target, self.package, "fzf")
        self.assertEqual(rex.call.call_count, 1)
        rex.action.assert_not_called()

    def test_detached_failure_is_displayed_in_invoking_client(self):
        with (
            patch.object(self.opener.sys, "argv", ["opener.py", "--session", "s", "--block", "b", "--client", "c"]),
            patch.object(self.opener, "Rex", return_value=Mock()),
            patch.object(self.opener, "context", return_value=({}, self.package, "c")),
            patch.object(self.opener, "open_text_picker", side_effect=RuntimeError("capture failed")),
            patch.object(self.opener, "popup", return_value=0) as popup,
        ):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                self.opener.main()
        self.assertEqual(popup.call_args.args[1:4], ("s", "b", "c"))
        self.assertIn("capture failed", popup.call_args.args[-1])

    @unittest.skipUnless(shutil.which("nvim"), "Neovim is needed for the Lua registration check")
    def test_registration_leaves_key_choice_to_the_user(self):
        script = self.package / "check.lua"
        script.write_text("""
local actions, bindings, launched = {}, {}, nil
rex = {
  exec = setmetatable({ start = function(argv, opts) launched = {argv=argv, opts=opts}; return {} end },
    { __call = function(_, argv) return {ok=true, stdout=argv[2] .. "\\n"} end }),
  action = function(action) actions[action.name] = action end,
  bind = function(key, action) bindings[key] = action end,
}
dofile(arg[1])({path="/custom/bin"})
assert(next(bindings) == nil, "Plugin must not bind keys")
local action = assert(actions.rex_opener_open, "Public action missing")
rex.bind("cmd+shift+o", action.name)
assert(bindings["cmd+shift+o"] == "rex_opener_open")
assert(not pcall(action.run, {}), "Must require an exact terminal")
action.run({session_id="session:one", block_id="block:one", client_id="client:one"})
assert(launched.opts.detach and launched.opts.env.PATH == "/custom/bin")
assert(launched.opts.env.NVIM == false)
assert(launched.argv[1] == "python3")
assert(launched.argv[2] == arg[1]:gsub("init.lua$", "opener.py"))
assert(launched.argv[3] == "--session" and launched.argv[4] == "session:one")
assert(launched.argv[7] == "--client" and launched.argv[8] == "client:one")
""")
        result = subprocess.run(
            ["nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l", str(script), str(self.package / "init.lua")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
