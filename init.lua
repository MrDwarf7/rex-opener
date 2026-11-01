-- Load with dofile("/path/to/rex-opener/init.lua")(); bind rex_opener_open yourself.
local source = debug.getinfo(1, "S").source:gsub("^@", "")
local resolved = assert(rex.exec({ "realpath", source }))
assert(resolved.ok, resolved.stderr)
local directory = resolved.stdout:gsub("%s+$", ""):gsub("/init%.lua$", "")

return function(options)
  options = options or {}
  rex.action {
    name = "rex_opener_open", title = "Open URL or file", category = "Opener",
    description = "Find URLs and existing files in the invoking terminal's scrollback.",
    keywords = "open url link file directory browser",
    args = { type = "object", properties = {}, additionalProperties = false },
    run = function(ctx)
      assert(ctx.session_id and ctx.block_id, "This action needs an exact terminal block")
      local argv = { "python3", directory .. "/opener.py", "--session", ctx.session_id, "--block", ctx.block_id }
      if ctx.client_id then
        argv[#argv + 1] = "--client"
        argv[#argv + 1] = ctx.client_id
      end
      local process, err = rex.exec.start(argv, { detach = true, env = {
        PATH = options.path, NVIM = false, OPENCODE_NVIM_GENERATION = false,
      } })
      assert(process, err)
      return true
    end,
  }
end
