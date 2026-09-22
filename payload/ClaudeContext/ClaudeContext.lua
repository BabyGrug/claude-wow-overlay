--[[
    Claude Context
    --------------
    Dumps character, location and quest log to ClaudeContextDB (a JSON string)
    on login, logout and quest/zone changes. A companion desktop app reads this
    SavedVariables file directly off disk -- this addon never touches the
    network itself, it only writes the same kind of file any other addon does.

    SavedVariables only actually hit disk on logout or /reload, so /claudesync
    (which forces a reload) is the fast way to refresh it mid-session.

    Written against C_QuestLog / C_Map, the only quest-log/map API WoW Forever
    (interface 16000+) ships -- confirmed via QuestMaster's Compat.lua, which
    hit "attempt to call a nil value" on the legacy globals on this client.
]]

local ADDON_NAME = ...

-- ============================================================================
-- Minimal JSON encoder (strings, numbers, booleans, arrays, flat objects --
-- everything this addon actually produces)
-- ============================================================================

local function jsonEscape(s)
    s = tostring(s)
    s = s:gsub("\\", "\\\\")
    s = s:gsub("\"", "\\\"")
    s = s:gsub("\n", "\\n")
    s = s:gsub("\r", "\\r")
    s = s:gsub("\t", "\\t")

    -- Remaining control bytes (0-31 minus the ones above, plus 127) are
    -- scanned byte-by-byte instead of matched against a "[...]" range
    -- pattern: a pattern with an embedded control byte -- even Lua's %c
    -- class, which also risks corrupting UTF-8 continuation bytes 128-191
    -- since it's locale-dependent -- is what crashed this addon in-game the
    -- first time. No pattern involved here, so there's nothing left to go
    -- wrong the same way.
    local out = {}
    for i = 1, #s do
        local b = s:byte(i)
        if b < 32 or b == 127 then
            table.insert(out, string.format("\\u%04x", b))
        else
            table.insert(out, s:sub(i, i))
        end
    end
    return table.concat(out)
end

local jsonEncode

local function jsonEncodeTable(t)
    local n = 0
    local isArray = true
    for k in pairs(t) do
        n = n + 1
        if type(k) ~= "number" then isArray = false end
    end
    if n == 0 then return "[]" end

    if isArray then
        local parts = {}
        for i = 1, n do
            parts[i] = jsonEncode(t[i])
        end
        return "[" .. table.concat(parts, ",") .. "]"
    end

    local parts = {}
    for k, v in pairs(t) do
        table.insert(parts, "\"" .. jsonEscape(tostring(k)) .. "\":" .. jsonEncode(v))
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

jsonEncode = function(value)
    local t = type(value)
    if t == "string" then
        return "\"" .. jsonEscape(value) .. "\""
    elseif t == "number" then
        if value ~= value then return "0" end -- NaN guard
        return tostring(value)
    elseif t == "boolean" then
        return value and "true" or "false"
    elseif t == "table" then
        return jsonEncodeTable(value)
    end
    return "null"
end

-- ============================================================================
-- Data collection
-- ============================================================================

-- Which game version this client actually is -- matters a lot, since quest
-- content, itemization and talents genuinely differ between them, and the
-- overlay app picks up whichever flavor's SavedVariables file was written
-- most recently (see overlay.py's find_wow_context_file()). Without this,
-- Claude could confidently answer a Retail question off stale Classic data
-- with no way to notice the mismatch.
--
-- WoW Forever's own interface/toc version (16000-19999) is checked FIRST and
-- confirmed via the same heuristic installer.py already uses successfully
-- to detect it (a distinct product, not just "some classic client"). The
-- WOW_PROJECT_* constants below are Blizzard's own official values for
-- telling retail/classic-era/etc. apart -- reliable for everything except
-- Forever specifically, which may or may not share an ID with an existing
-- tier (unconfirmed -- no live Retail/Classic client here to check against).
local function GetGameFlavor()
    local tocVersion = select(4, GetBuildInfo())
    if tocVersion and tocVersion >= 16000 and tocVersion <= 19999 then
        return "WoW Forever"
    end
    local id = WOW_PROJECT_ID
    if id == WOW_PROJECT_MAINLINE then return "Retail"
    elseif id == WOW_PROJECT_CLASSIC then return "Classic Era"
    elseif WOW_PROJECT_BURNING_CRUSADE_CLASSIC and id == WOW_PROJECT_BURNING_CRUSADE_CLASSIC then
        return "Burning Crusade Classic"
    elseif WOW_PROJECT_WRATH_CLASSIC and id == WOW_PROJECT_WRATH_CLASSIC then
        return "Wrath Classic"
    elseif WOW_PROJECT_CATACLYSM_CLASSIC and id == WOW_PROJECT_CATACLYSM_CLASSIC then
        return "Cataclysm Classic"
    elseif WOW_PROJECT_MISTS_CLASSIC and id == WOW_PROJECT_MISTS_CLASSIC then
        return "Mists of Pandaria Classic"
    else
        return "Unknown WoW version (internal ID " .. tostring(id) .. ")"
    end
end

local function GetCoords()
    local mapId = C_Map and C_Map.GetBestMapForUnit and C_Map.GetBestMapForUnit("player")
    if not mapId then return nil, nil, nil end
    local pos = C_Map.GetPlayerMapPosition and C_Map.GetPlayerMapPosition(mapId, "player")
    if not pos then return nil, nil, mapId end
    local x, y = pos:GetXY()
    return x, y, mapId
end

local function GetMapName(mapId)
    if not mapId or not C_Map or not C_Map.GetMapInfo then return nil end
    local info = C_Map.GetMapInfo(mapId)
    return info and info.name
end

-- BEST-EFFORT fallback for clients without C_QuestLog at all. Unlike the
-- C_QuestLog path below (verified via QuestMaster's Compat.lua on WoW
-- Forever), this hasn't been checked against a live Classic Era client or a
-- reference addon -- there's no such client installed here to test against.
-- Uses the pre-Legion legacy quest-log API, which is what Classic Era
-- addons historically needed. Needs real-world confirmation.
local function CollectQuestsLegacy()
    local quests = {}
    if not GetNumQuestLogEntries or not GetQuestLogTitle then return quests end
    local n = GetNumQuestLogEntries() or 0
    for i = 1, n do
        local title, level, _, isHeader, _, _, _, questID = GetQuestLogTitle(i)
        if not isHeader and questID and questID ~= 0 then
            local objectives = {}
            local numObj = GetNumQuestLeaderBoards and GetNumQuestLeaderBoards(i) or 0
            for j = 1, numObj do
                local text = GetQuestLogLeaderBoard and GetQuestLogLeaderBoard(j, i)
                if text and text ~= "" then
                    table.insert(objectives, text)
                end
            end
            table.insert(quests, {
                title = title or "",
                level = level or 0,
                questID = questID,
                objectives = objectives,
            })
        end
    end
    return quests
end

local function CollectQuests()
    if not (C_QuestLog and C_QuestLog.GetInfo) then
        return CollectQuestsLegacy()
    end

    local quests = {}
    -- Quests under a collapsed header are invisible to the scan -- expand
    -- everything first. Safe to call on non-headers too.
    local n = C_QuestLog.GetNumQuestLogEntries() or 0
    for i = 1, n do
        if C_QuestLog.ExpandQuestHeader then C_QuestLog.ExpandQuestHeader(i) end
    end

    n = C_QuestLog.GetNumQuestLogEntries() or 0
    for i = 1, n do
        local info = C_QuestLog.GetInfo(i)
        if info and not info.isHeader and info.questID and info.questID ~= 0 then
            local objectives = {}
            local numObj = C_QuestLog.GetNumQuestObjectives and C_QuestLog.GetNumQuestObjectives(info.questID) or 0
            for j = 1, numObj do
                local text
                if GetQuestObjectiveInfo then
                    text = GetQuestObjectiveInfo(info.questID, j, false)
                end
                if not text and C_QuestLog.GetQuestObjectives then
                    local objs = C_QuestLog.GetQuestObjectives(info.questID)
                    local obj = objs and objs[j]
                    text = obj and obj.text
                end
                if text and text ~= "" then
                    table.insert(objectives, text)
                end
            end
            table.insert(quests, {
                title = info.title or "",
                level = info.level or 0,
                questID = info.questID,
                objectives = objectives,
            })
        end
    end
    return quests
end

-- Slot API name -> display label. GetInventorySlotInfo/GetInventoryItemLink/
-- GetItemInfo are foundational globals that predate the quest-log-style API
-- overhaul (unlike C_QuestLog, there's no known removal of these on this
-- client) -- no legacy/modern branching needed here.
local EQUIP_SLOTS = {
    {"HeadSlot", "Head"}, {"NeckSlot", "Neck"}, {"ShoulderSlot", "Shoulder"},
    {"BackSlot", "Back"}, {"ChestSlot", "Chest"}, {"WristSlot", "Wrist"},
    {"HandsSlot", "Hands"}, {"WaistSlot", "Waist"}, {"LegsSlot", "Legs"},
    {"FeetSlot", "Feet"}, {"Finger0Slot", "Ring 1"}, {"Finger1Slot", "Ring 2"},
    {"Trinket0Slot", "Trinket 1"}, {"Trinket1Slot", "Trinket 2"},
    {"MainHandSlot", "Main Hand"}, {"SecondaryHandSlot", "Off Hand"},
    {"RangedSlot", "Ranged"},
}

-- Best-effort item name/ilvl lookup. GetItemInfo turned out to be NIL on
-- this client -- confirmed the hard way (a real "attempt to call a nil
-- value" in-game on load/reload, unlike C_QuestLog/C_Container which were
-- verified against QuestMaster's source before shipping). Tries the modern
-- C_Item table first, then the classic global, then just falls back to the
-- raw link -- same defensive-across-API-surfaces approach QuestMaster's own
-- code uses for functions it isn't sure about either.
local function GetItemDisplayInfo(itemLink)
    if not itemLink then return nil, nil end
    local getInfo = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    if not getInfo then
        return itemLink, nil
    end
    local ok, name, _, _, ilvl = pcall(getInfo, itemLink)
    if not ok or not name then
        return itemLink, nil
    end
    return name, ilvl
end

local function CollectEquipped()
    local items = {}
    for _, entry in ipairs(EQUIP_SLOTS) do
        local apiSlot, label = entry[1], entry[2]
        local slotId = GetInventorySlotInfo and GetInventorySlotInfo(apiSlot)
        if slotId then
            local itemLink = GetInventoryItemLink("player", slotId)
            if itemLink then
                local name, ilvl = GetItemDisplayInfo(itemLink)
                table.insert(items, {
                    slot = label,
                    name = name,
                    ilvl = ilvl,
                })
            end
        end
    end
    return items
end

-- BEST-EFFORT: WoW Forever appears to have its own "Legacy Talent System"
-- (per Blizzard's own BlizzCon materials) rather than the classic 3-tree
-- talent panel, and there's no reference addon on this client using either
-- API to verify against (unlike quest logs/bags, which QuestMaster proved).
-- This tries the classic API and simply omits talent info if it doesn't
-- exist rather than guessing -- needs in-game confirmation of whether it
-- ever actually populates on this client.
local function CollectTalents()
    if not (GetNumTalentTabs and GetTalentTabInfo) then
        return nil
    end
    local trees = {}
    local numTabs = GetNumTalentTabs() or 0
    for tab = 1, numTabs do
        local name, _, pointsSpent = GetTalentTabInfo(tab)
        if name and pointsSpent and pointsSpent > 0 then
            table.insert(trees, name .. ": " .. pointsSpent)
        end
    end
    if #trees == 0 then return nil end
    return table.concat(trees, ", ")
end

-- Same C_Container-with-legacy-fallback pattern QuestMaster itself uses on
-- this client (confirmed via its source) -- proven, not guessed.
local function CollectBagsAndGold()
    local money = GetMoney and GetMoney() or 0
    local itemNames = {}
    local numBags = NUM_BAG_SLOTS or 4
    for bag = 0, numBags do
        local numSlots
        if C_Container and C_Container.GetContainerNumSlots then
            numSlots = C_Container.GetContainerNumSlots(bag)
        elseif GetContainerNumSlots then
            numSlots = GetContainerNumSlots(bag)
        end
        for slot = 1, (numSlots or 0) do
            local itemLink
            if C_Container and C_Container.GetContainerItemInfo then
                local info = C_Container.GetContainerItemInfo(bag, slot)
                itemLink = info and info.hyperlink
            elseif GetContainerItemLink then
                itemLink = GetContainerItemLink(bag, slot)
            end
            if itemLink then
                local name = GetItemDisplayInfo(itemLink)
                table.insert(itemNames, name)
            end
        end
    end
    return {
        gold = math.floor(money / 10000),
        silver = math.floor((money % 10000) / 100),
        copper = money % 100,
        items = itemNames,
    }
end

local function BuildContext()
    local localizedClass, englishClass = UnitClass("player")
    local localizedRace, englishRace = UnitRace("player")
    local faction = UnitFactionGroup("player")
    local x, y, mapId = GetCoords()

    local location = {
        zone = GetZoneText() or "",
        subzone = GetSubZoneText() or "",
        mapName = GetMapName(mapId),
    }
    if x then location.x = math.floor(x * 1000 + 0.5) / 10 end
    if y then location.y = math.floor(y * 1000 + 0.5) / 10 end

    local context = {
        savedAt = date("%Y-%m-%d %H:%M:%S"),
        gameVersion = GetGameFlavor(),
        character = {
            name = UnitName("player") or "",
            realm = GetRealmName() or "",
            class = englishClass or localizedClass or "",
            race = englishRace or localizedRace or "",
            level = UnitLevel("player") or 0,
            faction = faction or "Neutral",
        },
        location = location,
        quests = CollectQuests(),
        equipped = CollectEquipped(),
        bags = CollectBagsAndGold(),
    }

    local talents = CollectTalents()
    if talents then
        context.talents = talents
    end

    return context
end

local function Sync()
    -- Both steps guarded together: an encoding error is just as much a
    -- "degrade quietly" case as a data-collection error.
    local ok, encoded = pcall(function()
        return jsonEncode(BuildContext())
    end)
    if ok then
        ClaudeContextDB = encoded
    else
        -- TEMPORARY: surface the real reason instead of failing quietly,
        -- while we're actively debugging why nothing gets saved.
        print("|cffff6b6bClaude Context sync failed:|r " .. tostring(encoded))
    end
end

-- ============================================================================
-- Triggers
-- ============================================================================

-- Confirmed directly, in-game: the very first sync right after login/reload
-- can report equipped gear (and presumably bag contents) as completely
-- empty even though the player clearly has items equipped -- WoW's
-- inventory/equipment cache isn't always populated the instant
-- PLAYER_LOGIN/PLAYER_ENTERING_WORLD fires. A /claudesync run later in the
-- same session (well after everything's settled) found the same gear just
-- fine, which pins this down as a startup race, not a wrong API. A couple
-- seconds' delay before the FIRST post-load sync avoids it; QUEST_LOG_UPDATE
-- and ZONE_CHANGED_NEW_AREA fire well into an already-loaded session so they
-- don't need it.
local function DelayedSync()
    C_Timer.After(2, Sync)
end

local f = CreateFrame("Frame")
f:RegisterEvent("PLAYER_LOGIN")
-- PLAYER_LOGIN only fires on a genuine fresh login, not on /reload -- without
-- this, /reload never re-syncs, which is exactly what left ClaudeContextDB
-- stuck at nil after the jsonEscape fix (the fixed code never actually ran).
f:RegisterEvent("PLAYER_ENTERING_WORLD")
f:RegisterEvent("PLAYER_LOGOUT")
f:RegisterEvent("QUEST_LOG_UPDATE")
f:RegisterEvent("ZONE_CHANGED_NEW_AREA")
f:SetScript("OnEvent", function(self, event, ...)
    if event == "PLAYER_LOGIN" or event == "PLAYER_ENTERING_WORLD" then
        DelayedSync()
    else
        Sync()
    end
end)

SLASH_CLAUDECONTEXT1 = "/claudesync"
SlashCmdList["CLAUDECONTEXT"] = function()
    Sync()
    -- Deliberately NOT calling ReloadUI() here. It's a protected call, and
    -- even called synchronously and directly in this handler it was still
    -- getting silently blocked (no error, but nothing actually reloaded, so
    -- SavedVariables never got flushed to disk). Rather than fight taint
    -- rules from inside addon code, just tell the player to do it themselves
    -- -- typing /reload directly is never subject to this at all.
    print("|cff7c5cffClaude Context:|r saved to memory. Type |cffffffff/reload|r "
        .. "yourself now to write it to disk.")
end

-- ============================================================================
-- /claudemark [zone name] x y -- places a native waypoint pin (the same one
-- shift-click on the map gives you) from a location the overlay app found.
-- Confirmed working on this client: C_Map.SetUserWaypoint exists here even
-- though it's a Legion-era retail API, unlike C_QuestLog's legacy globals.
-- ============================================================================

-- Resolves a zone name to a mapID by walking up from the player's current
-- map to a likely top-level root, then searching that root's descendants.
-- This covers "same continent as the player" reliably without needing to
-- know a hardcoded "world root" mapID, which differs across game versions.
local function FindMapIdByName(name)
    if not name or name == "" then return nil end
    name = name:lower()

    local rootId = C_Map.GetBestMapForUnit("player")
    for _ = 1, 6 do
        if not rootId then break end
        local info = C_Map.GetMapInfo(rootId)
        if not info or not info.parentMapID or info.parentMapID == 0 then break end
        rootId = info.parentMapID
    end
    if not rootId then return nil end

    local rootInfo = C_Map.GetMapInfo(rootId)
    if rootInfo and rootInfo.name and rootInfo.name:lower() == name then
        return rootId
    end

    -- Manual recursion rather than GetMapChildrenInfo's allDescendants flag --
    -- HereBeDragons (bundled with QuestMaster, a proven library on this exact
    -- client) does the same, which suggests that flag isn't trustworthy here.
    local function searchChildren(parentId, depth)
        if depth > 4 then return nil end
        local children = C_Map.GetMapChildrenInfo(parentId)
        if not children then return nil end
        for _, child in ipairs(children) do
            if child.name and child.name:lower() == name then
                return child.mapID
            end
        end
        for _, child in ipairs(children) do
            local found = searchChildren(child.mapID, depth + 1)
            if found then return found end
        end
        return nil
    end

    return searchChildren(rootId, 0)
end

SLASH_CLAUDEMARK1 = "/claudemark"
SlashCmdList["CLAUDEMARK"] = function(msg)
    local parts = {}
    for word in tostring(msg or ""):gmatch("%S+") do
        table.insert(parts, word)
    end

    if #parts < 2 then
        print("|cffff6b6bClaude Context:|r usage: /claudemark [zone name] x y")
        return
    end

    local y = tonumber(parts[#parts])
    local x = tonumber(parts[#parts - 1])
    if not x or not y then
        print("|cffff6b6bClaude Context:|r couldn't parse coordinates from: " .. msg)
        return
    end

    local zoneName = table.concat(parts, " ", 1, #parts - 2)
    local mapId = FindMapIdByName(zoneName)
    local usedFallback = false
    if not mapId then
        mapId = C_Map.GetBestMapForUnit("player")
        usedFallback = true
    end

    if not mapId or not C_Map.SetUserWaypoint or not UiMapPoint
        or not UiMapPoint.CreateFromCoordinates then
        print("|cffff6b6bClaude Context:|r couldn't set a waypoint on this client.")
        return
    end

    local point = UiMapPoint.CreateFromCoordinates(mapId, x / 100, y / 100)
    C_Map.SetUserWaypoint(point)
    if C_SuperTrack and C_SuperTrack.SetSuperTrackedUserWaypoint then
        C_SuperTrack.SetSuperTrackedUserWaypoint(true)
    end

    if usedFallback and zoneName ~= "" then
        print(string.format(
            "|cffff6b6bClaude Context:|r couldn't find zone \"%s\" nearby -- "
            .. "marked (%.1f, %.1f) in your current zone instead.",
            zoneName, x, y))
    else
        print(string.format(
            "|cff7c5cffClaude Context:|r waypoint set: %s (%.1f, %.1f)",
            zoneName ~= "" and zoneName or "your current zone", x, y))
    end
end
