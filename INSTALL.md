# Installing Rex opener

Open URLs and local files found in a Rex terminal's output. No dotfiles checkout,
Python packages or plugin manager is required.

For a quick start, supported targets and troubleshooting, see [README.md](README.md).

## Requirements

- macOS and a local Rex server. Tested with bundled CLI
  `0.0.1793420730-g6d0d9a`; Rex Beta APIs can change.
- Python 3.10+ and standard macOS command-line utilities.
- Fish and fzf for pickers; Neovim for text files; Yazi for directories.
- Rex CLI on the server's `PATH`, or `REX_BIN_DIR` pointing to its directory.
- Enable **Settings → Rex Server → Remote Control** for targeted app focus.

## Load and choose your keys

Clone the repository somewhere permanent (Fish):

```fish
git clone https://github.com/shaheislam/rex-opener.git ~/.config/rex/plugins/rex-opener
```

Add this to your Rex `init.lua`, replacing the example path with the clone's
absolute location:

```lua
dofile("/absolute/path/to/rex-opener/init.lua")()
rex.bind("ctrl+s>o", "rex_opener_open")
```

The plugin never calls `rex.bind`. Choose any Rex key or leader sequence, such
as `cmd+shift+o`, or omit the binding and use **Open URL or file** in the command
palette. The public action name is `rex_opener_open`.

Rex's GUI server may have a smaller `PATH` than your terminal. If required,
provide it explicitly when loading the plugin (include Python, Rex, Fish, fzf,
Neovim and Yazi):

```lua
dofile("/absolute/path/to/rex-opener/init.lua")({
  path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
})
rex.bind("cmd+shift+o", "rex_opener_open")
```

Validate, then reload from Fish:

```fish
rex config check
rex --autostart=false config reload
```

Reloading is required: an already-running server keeps its previous actions and
bindings until it reads the configuration again. After an update, reload even
when the `init.lua` entry itself has not changed.

## Behaviour

- Reads the invoking terminal's paginated output, including scrollback.
- Recognizes URLs, bare domains, Markdown links, existing local paths, spaces,
  punctuation and `file:line:column`. Only the line is passed to Neovim.
- One target opens immediately; multiple targets use fzf. Escape cancels.
- Text and Markdown open in a new adaptive Neovim split at the requested line.
- Directories open in Yazi; videos prefer IINA when installed; URLs and other
  files use macOS `open` and the default application.
- Pickers use the invoking Neovim's floating terminal when available, otherwise
  a native Rex layer in the invoking tab. Other apps viewing that session can see
  the picker. Editor splits do not redirect another app's focus.
- Targets are ordered newest first, keeping the latest occurrence of duplicates.

Targets come from terminal output; select only URLs and files you intend to
open. Remote-server panes and other users' terminals are rejected. The plugin
does not reuse an existing editor for text targets or prioritize the viewport.

## Check a standalone copy

From this directory:

```fish
python3 test_opener.py
```

Tests copy the plugin to a temporary location with spaces and an apostrophe,
then check parsing, capture, dispatch and user-owned keybindings without
connecting to a live Rex server. Neovim is needed for the Lua registration test.
