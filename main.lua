-- 3DS Save Sync for muOS
-- Push or pull DraStic saves to/from a 3DS running ftpd (TWiLight Menu++ layout).
-- Transfers are done by sync3ds.py; this file is only the UI.

local PORT = 5000
local DEFAULT_IP = "192.168.1.120"
local LOCAL_SAVE_DIR = "/mnt/mmc/MUOS/save/drastic/backup"
local ROM_DIRS = {
    "/mnt/sdcard/ROMS/DS",
    "/mnt/sdcard/ROMS/NDS",
    "/mnt/mmc/ROMS/DS",
    "/mnt/mmc/ROMS/NDS",
}
local PYTHON = "/usr/bin/python3"
local ROWS = 11
local SLOTS = 10            -- TWiLight save numbers: 0 = .sav, N = .savN

local appDir = love.filesystem.getSource()
local configPath = appDir .. "/config.ini"

local state = "list"        -- "list" | "ip" | "game" | "confirm" | "working" | "result"
local ip = DEFAULT_IP

local games = {}            -- { base = "...", hasLocal = bool }
local gameIndex = 1
local gameScroll = 0

local octets = { 0, 0, 0, 0 }
local octetIndex = 1

local current = nil         -- selected game
local remote = nil          -- result of "status" for the selected game
local options = {}
local optionIndex = 1
local pendingAction = nil   -- "push" | "pull"
local slot = 0              -- 3DS save slot to push to / pull from
local slotSizes = {}        -- [slot] = bytes, for slots that exist on the 3DS
local twilightSlot = 0      -- slot TWiLight is currently set to for this game

local job = nil             -- { label, run, frames }
local result = nil          -- { ok, title, msg, backup, extra }

local status = ""
local statusUntil = 0
local font, fontSmall, fontBig

-- ---------------------------------------------------------------- helpers

local function setStatus(text)
    status = text
    statusUntil = love.timer.getTime() + 3
end

local function trim(value)
    return (value:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function basename(path)
    return path:match("([^/]+)$") or path
end

local function shq(value)
    return "'" .. value:gsub("'", "'\\''") .. "'"
end

local function shorten(text, max)
    if #text > max then return text:sub(1, max - 3) .. "..." end
    return text
end

local function kb(bytes)
    if bytes == nil or bytes < 0 then return "none" end
    if bytes >= 1024 then return math.floor(bytes / 1024) .. " KB" end
    return bytes .. " B"
end

local function loadConfig()
    local handle = io.open(configPath, "r")
    if not handle then return end
    for line in handle:lines() do
        local value = line:match("^ip=(.+)$")
        if value then ip = trim(value) end
    end
    handle:close()
end

local function saveConfig()
    local handle = io.open(configPath, "w")
    if not handle then return false end
    handle:write("ip=", ip, "\n")
    handle:close()
    return true
end

local function listFiles(dir, ext)
    local found = {}
    local pipe = io.popen('ls -1 ' .. shq(dir) .. ' 2>/dev/null')
    if pipe then
        for line in pipe:lines() do
            local name = trim(line)
            local base = name:match("^(.*)%." .. ext .. "$")
            if base then found[#found + 1] = base end
        end
        pipe:close()
    end
    return found
end

-- Every game with a local save or a local ROM (so a save can be pulled for it).
local function listGames()
    local byBase, list = {}, {}
    local function add(base, hasLocal)
        local game = byBase[base]
        if not game then
            game = { base = base, hasLocal = false }
            byBase[base] = game
            list[#list + 1] = game
        end
        game.hasLocal = game.hasLocal or hasLocal
    end
    for _, base in ipairs(listFiles(LOCAL_SAVE_DIR, "sav")) do add(base, true) end
    for _, base in ipairs(listFiles(LOCAL_SAVE_DIR, "dsv")) do add(base, true) end
    for _, dir in ipairs(ROM_DIRS) do
        for _, base in ipairs(listFiles(dir, "nds")) do add(base, false) end
    end
    table.sort(list, function(a, b)
        if a.hasLocal ~= b.hasLocal then return a.hasLocal end
        return a.base:lower() < b.base:lower()
    end)
    return list
end

-- Run the backend and parse its key=value output.
local function runBackend(action, base, saveSlot)
    local cmd = PYTHON .. " " .. shq(appDir .. "/sync3ds.py") .. " " .. action .. " " .. shq(ip)
    if base then cmd = cmd .. " " .. shq(base) end
    if saveSlot then cmd = cmd .. " " .. saveSlot end
    local out = { ok = false, msg = "No response from sync3ds.py", extra = {} }
    local pipe = io.popen(cmd .. " 2>&1")
    if not pipe then
        out.msg = "Could not start python3"
        return out
    end
    for line in pipe:lines() do
        local key, value = line:match("^([%w_]+)=(.*)$")
        if key == "ok" then
            out.ok = value == "1"
        elseif key then
            out[key] = value
        elseif trim(line) ~= "" then
            out.extra[#out.extra + 1] = line
        end
    end
    pipe:close()
    return out
end

-- Show a "working" frame, then run fn on the next update.
local function startJob(label, fn)
    job = { label = label, run = fn, frames = 0 }
    state = "working"
end

local function showResult(ok, title, res)
    result = {
        ok = ok,
        title = title,
        msg = res.msg or "",
        backup = res.backup,
        extra = res.extra or {},
    }
    state = "result"
end

local function clampScroll(index, scroll, total)
    if index < scroll + 1 then
        scroll = index - 1
    elseif index > scroll + ROWS then
        scroll = index - ROWS
    end
    if scroll < 0 then scroll = 0 end
    local maxScroll = math.max(0, total - ROWS)
    if scroll > maxScroll then scroll = maxScroll end
    return scroll
end

local function moveGame(delta)
    if #games == 0 then return end
    gameIndex = math.max(1, math.min(#games, gameIndex + delta))
    gameScroll = clampScroll(gameIndex, gameScroll, #games)
end

-- ---------------------------------------------------------------- screens

local function openIpEditor()
    local a, b, c, d = ip:match("^(%d+)%.(%d+)%.(%d+)%.(%d+)$")
    octets = { tonumber(a) or 192, tonumber(b) or 168, tonumber(c) or 1, tonumber(d) or 129 }
    octetIndex = 4
    state = "ip"
end

-- keepSlot: stay on the slot the user picked (used when refreshing after a transfer).
local function openGame(game, keepSlot)
    current = game
    remote = nil
    startJob("Checking the 3DS...", function()
        remote = runBackend("status", game.base)
        slotSizes = {}
        for i = 0, SLOTS - 1 do
            local size = tonumber(remote["slot_" .. i] or "")
            if size then slotSizes[i] = size end
        end
        twilightSlot = tonumber(remote.twilight_slot or "0") or 0
        if not keepSlot then slot = twilightSlot end
        options = {
            { id = "slot" },
            { id = "push", label = "PUSH   this device  ->  3DS" },
            { id = "pull", label = "PULL   3DS  ->  this device" },
            { id = "back", label = "BACK" },
        }
        if not keepSlot then optionIndex = 2 end
        state = "game"
    end)
end

local function slotFile(n)
    return n == 0 and ".sav" or (".sav" .. n)
end

-- Size of the selected slot on the 3DS: bytes, -1 if empty, nil if unknown.
local function remoteSize()
    if not (remote and remote.ok) then return nil end
    return slotSizes[slot] or -1
end

local function changeSlot(delta)
    slot = math.max(0, math.min(SLOTS - 1, slot + delta))
end

local function chooseOption()
    local option = options[optionIndex]
    if not option then return end
    if option.id == "back" then
        state = "list"
        return
    end
    if option.id == "slot" then
        changeSlot(slot == SLOTS - 1 and -slot or 1)   -- A cycles 0..9
        return
    end
    if option.id == "push" and not current.hasLocal then
        setStatus("NO SAVE ON THIS DEVICE TO PUSH")
        return
    end
    if option.id == "pull" and remoteSize() == -1 then
        setStatus("3DS SLOT " .. slot .. " (" .. slotFile(slot) .. ") IS EMPTY")
        return
    end
    pendingAction = option.id
    state = "confirm"
end

local function runPending()
    local action = pendingAction
    local label = action == "push" and ("Sending save to 3DS slot " .. slot .. "...")
        or ("Copying 3DS slot " .. slot .. " to this device...")
    startJob(label, function()
        local res = runBackend(action, current.base, slot)
        local title = (action == "push" and "PUSH" or "PULL") .. (res.ok and " COMPLETE" or " FAILED")
        if res.ok and action == "pull" then current.hasLocal = true end
        showResult(res.ok, title, res)
    end)
end

-- ---------------------------------------------------------------- love

function love.load()
    love.graphics.setBackgroundColor(0.07, 0.08, 0.11)
    font = love.graphics.newFont(16)
    fontSmall = love.graphics.newFont(12)
    fontBig = love.graphics.newFont(28)
    loadConfig()
    games = listGames()
    if #games == 0 then
        setStatus("NO DS SAVES OR ROMS FOUND")
    end
end

function love.update()
    if job then
        job.frames = job.frames + 1
        if job.frames >= 2 then      -- make sure the working frame was drawn
            local run = job.run
            job = nil
            run()
        end
    end
end

function love.keypressed(key)
    if state == "working" then return end

    if state == "result" then
        if key == "return" or key == "escape" then
            state = current and "game" or "list"
            if current and result and result.ok then openGame(current, true) end
        end
        return
    end

    if state == "ip" then
        local step = 0
        if key == "up" then step = 1
        elseif key == "down" then step = -1
        elseif key == "r" then step = 10
        elseif key == "l" then step = -10
        elseif key == "left" then octetIndex = math.max(1, octetIndex - 1)
        elseif key == "right" then octetIndex = math.min(4, octetIndex + 1)
        elseif key == "return" then
            ip = table.concat(octets, ".")
            if saveConfig() then setStatus("3DS IP SET TO " .. ip) else setStatus("IP SET (COULD NOT SAVE CONFIG)") end
            state = "list"
        elseif key == "escape" then
            state = "list"
            setStatus("IP UNCHANGED")
        end
        if step ~= 0 then
            octets[octetIndex] = (octets[octetIndex] + step) % 256
        end
        return
    end

    if state == "confirm" then
        if key == "return" then
            runPending()
        elseif key == "escape" then
            state = "game"
            setStatus("CANCELLED")
        end
        return
    end

    if state == "game" then
        if key == "up" then optionIndex = math.max(1, optionIndex - 1)
        elseif key == "down" then optionIndex = math.min(#options, optionIndex + 1)
        elseif key == "left" then changeSlot(-1)
        elseif key == "right" then changeSlot(1)
        elseif key == "return" then chooseOption()
        elseif key == "escape" then state = "list"
        end
        return
    end

    -- list screen
    if key == "escape" then love.event.quit() return end
    if key == "up" then moveGame(-1) return end
    if key == "down" then moveGame(1) return end
    if key == "left" then moveGame(-ROWS) return end
    if key == "right" then moveGame(ROWS) return end
    if key == "i" then openIpEditor() return end
    if key == "t" then
        current = nil
        startJob("Connecting to " .. ip .. ":" .. PORT .. "...", function()
            local res = runBackend("ping")
            showResult(res.ok, res.ok and "3DS CONNECTED" or "3DS NOT REACHABLE", res)
        end)
        return
    end
    if key == "return" and games[gameIndex] then
        openGame(games[gameIndex])
    end
end

local function drawHeader(title, subtitle)
    love.graphics.setColor(0.13, 0.15, 0.20)
    love.graphics.rectangle("fill", 0, 0, 640, 52)
    love.graphics.setColor(0.95, 0.96, 1.0)
    love.graphics.setFont(font)
    love.graphics.print(title, 14, 10)
    love.graphics.setColor(0.55, 0.60, 0.70)
    love.graphics.setFont(fontSmall)
    love.graphics.print(subtitle, 14, 32)
end

local function drawFooter(hints)
    love.graphics.setColor(0.13, 0.15, 0.20)
    love.graphics.rectangle("fill", 0, 438, 640, 42)
    love.graphics.setFont(fontSmall)
    if love.timer.getTime() < statusUntil and status ~= "" then
        love.graphics.setColor(0.45, 0.85, 0.55)
        love.graphics.print(status, 14, 446)
    else
        love.graphics.setColor(0.55, 0.60, 0.70)
        love.graphics.print(hints, 14, 446)
    end
end

local function drawRow(y, selected)
    if selected then
        love.graphics.setColor(0.20, 0.40, 0.65)
        love.graphics.rectangle("fill", 8, y - 4, 624, 30)
    end
end

local function drawList()
    drawHeader("3DS SAVE SYNC", "3DS " .. ip .. ":" .. PORT .. "     " .. #games .. " DS games")
    love.graphics.setFont(fontSmall)
    for row = 1, ROWS do
        local index = gameScroll + row
        local game = games[index]
        if not game then break end
        local y = 60 + (row - 1) * 33
        drawRow(y, index == gameIndex)
        if game.hasLocal then
            love.graphics.setColor(0.40, 0.85, 0.50)
            love.graphics.print("[SAVE]", 18, y + 4)
            love.graphics.setColor(0.95, 0.96, 1.0)
        else
            love.graphics.setColor(0.45, 0.48, 0.55)
            love.graphics.print("[ -- ]", 18, y + 4)
            love.graphics.setColor(0.60, 0.63, 0.70)
        end
        love.graphics.print(shorten(game.base, 82), 72, y + 4)
    end
    drawFooter("A SELECT   X EDIT 3DS IP   Y TEST 3DS   B EXIT")
end

local function drawIp()
    drawHeader("3DS IP ADDRESS", "Port " .. PORT .. " is fixed")
    love.graphics.setFont(fontBig)
    local x = 110
    for i = 1, 4 do
        local text = string.format("%3d", octets[i])
        if i == octetIndex then
            love.graphics.setColor(0.20, 0.40, 0.65)
            love.graphics.rectangle("fill", x - 8, 196, 84, 48)
            love.graphics.setColor(1, 1, 1)
        else
            love.graphics.setColor(0.72, 0.76, 0.84)
        end
        love.graphics.print(text, x, 204)
        if i < 4 then
            love.graphics.setColor(0.55, 0.60, 0.70)
            love.graphics.print(".", x + 84, 204)
        end
        x = x + 108
    end
    drawFooter("LEFT/RIGHT FIELD  UP/DOWN +-1  L1/R1 -+10  A SAVE  B CANCEL")
end

local function drawGame()
    drawHeader(shorten(current.base, 70), "Choose what to do with this save")
    love.graphics.setFont(font)
    love.graphics.setColor(0.72, 0.76, 0.84)
    love.graphics.print("This device:", 18, 72)
    love.graphics.print("3DS slots:", 18, 100)
    love.graphics.setColor(0.95, 0.96, 1.0)
    local localText = "no save"
    if current.hasLocal then
        localText = "DraStic save present"
        if remote and remote.local_format and remote.local_format ~= "none" then
            localText = localText .. " (." .. remote.local_format .. ", " .. kb(tonumber(remote.local_size)) .. ")"
        end
    end
    love.graphics.print(localText, 150, 72)
    if remote and remote.ok then
        local used = {}
        for i = 0, SLOTS - 1 do
            if slotSizes[i] then used[#used + 1] = i .. " (" .. kb(slotSizes[i]) .. ")" end
        end
        love.graphics.print(#used > 0 and table.concat(used, "   ") or "none yet (ROM found)", 150, 100)
        love.graphics.setColor(0.55, 0.60, 0.70)
        love.graphics.print("TWiLight is set to save slot " .. twilightSlot .. " for this game", 150, 128)
    else
        love.graphics.setColor(0.90, 0.45, 0.45)
        love.graphics.printf(remote and remote.msg or "unknown", 150, 100, 470)
    end

    for i, option in ipairs(options) do
        local y = 176 + (i - 1) * 40
        local selected = i == optionIndex
        drawRow(y, selected)
        love.graphics.setColor(selected and 1 or 0.72, selected and 1 or 0.76, selected and 1 or 0.84)
        local label = option.label
        if option.id == "slot" then
            local size = remoteSize()
            local info = size == nil and "" or (size >= 0 and kb(size) or "empty")
            label = "3DS SAVE SLOT   < " .. slot .. " >   " .. slotFile(slot) .. "   " .. info
        end
        love.graphics.print(label, 24, y)
    end
    drawFooter("LEFT/RIGHT SAVE SLOT   A CHOOSE   B BACK")
end

local function drawConfirm()
    local push = pendingAction == "push"
    drawHeader(push and "CONFIRM PUSH" or "CONFIRM PULL", shorten(current.base, 90))
    love.graphics.setFont(font)
    love.graphics.setColor(0.95, 0.96, 1.0)
    local lines
    if push then
        local size = remoteSize()
        lines = {
            "Send this device's save to 3DS slot " .. slot .. " (" .. slotFile(slot) .. ")?",
            "",
            size == -1 and "That slot is empty on the 3DS - a new save file is created."
                or "The 3DS save in that slot will be REPLACED.",
            size == -1 and "" or "Its current copy is backed up on this device first.",
        }
        if slot ~= twilightSlot then
            lines[#lines + 1] = "Note: TWiLight is set to slot " .. twilightSlot .. " - change it to play this one."
        end
    else
        lines = {
            "Copy 3DS slot " .. slot .. " (" .. slotFile(slot) .. ") to this device?",
            "",
            current.hasLocal and "This device's DraStic save will be REPLACED."
                or "This device has no save for this game yet.",
            current.hasLocal and "Its current copy is backed up first." or "",
        }
    end
    for i, line in ipairs(lines) do
        love.graphics.print(line, 24, 90 + (i - 1) * 30)
    end
    love.graphics.setColor(0.95, 0.80, 0.40)
    love.graphics.print("Close DraStic on this device before syncing.", 24, 250)
    drawFooter("A YES, DO IT   B NO")
end

local function drawWorking()
    drawHeader("3DS SAVE SYNC", "3DS " .. ip .. ":" .. PORT)
    love.graphics.setFont(fontBig)
    love.graphics.setColor(0.95, 0.96, 1.0)
    love.graphics.printf(job and job.label or "Working...", 20, 210, 600, "center")
    drawFooter("PLEASE WAIT")
end

local function drawResult()
    drawHeader("3DS SAVE SYNC", current and shorten(current.base, 90) or ("3DS " .. ip .. ":" .. PORT))
    if result.ok then
        love.graphics.setColor(0.18, 0.45, 0.25)
    else
        love.graphics.setColor(0.55, 0.18, 0.18)
    end
    love.graphics.rectangle("fill", 20, 80, 600, 70)
    love.graphics.setColor(1, 1, 1)
    love.graphics.setFont(fontBig)
    love.graphics.printf(result.title, 20, 98, 600, "center")

    love.graphics.setFont(font)
    love.graphics.setColor(0.95, 0.96, 1.0)
    love.graphics.printf(result.msg, 24, 175, 592)
    love.graphics.setFont(fontSmall)
    local y = 250
    if result.backup then
        love.graphics.setColor(0.55, 0.60, 0.70)
        love.graphics.printf("Previous copy backed up to: " .. result.backup, 24, y, 592)
        y = y + 40
    end
    love.graphics.setColor(0.90, 0.45, 0.45)
    for i = math.max(1, #result.extra - 5), #result.extra do
        love.graphics.print(shorten(result.extra[i], 95), 24, y)
        y = y + 16
    end
    drawFooter("A CONTINUE")
end

function love.draw()
    if state == "list" then drawList()
    elseif state == "ip" then drawIp()
    elseif state == "game" then drawGame()
    elseif state == "confirm" then drawConfirm()
    elseif state == "working" then drawWorking()
    elseif state == "result" then drawResult()
    end
end
