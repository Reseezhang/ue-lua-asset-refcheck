local Main = {}

local HERO_MESH = "/Game/Character/SKM_Hero"
local LEVEL_PATH = "/Game/Maps/L_Main"
local PANEL_CLASS = "/Game/Art/UI/WBP_Login.WBP_Login_C"

-- 注释里写路径不会被执行，工具应当把它排除掉
-- local OLD = "/Game/Art/UI/T_CommentedOut"

function Main.loadStatic()
    UE.UObject.Load(HERO_MESH)
    UE.UClass.Load(PANEL_CLASS)
end

function Main.loadDynamic(level)
    -- 字符串拼接：静态分析求不出值，工具只会标成「疑似拼接」
    local icon = "/Game/Art/UI/T_Icon_" .. level
    local fmt = string.format("/Game/Art/UI/T_Lv_%d", level)
    return icon, fmt
end

function Main.openLevel()
    UE.UObject.Load(LEVEL_PATH)
end

function Main.preload()
    UE.UObject.Load("/Game/Art/UI/T_Cloth_0")
    UE.UObject.Load("/Game/Art/UI/T_Cloth_1")
    UE.UObject.Load("/Game/Character/SK_Hero")
    UE.UObject.Load("/Game/Art/UI/WBP_Login")
end

function Main.loadDeadRef()
    -- 磁盘上并不存在这个资源
    UE.UObject.Load("/Game/Art/UI/T_NotExistAtAll")
end

return Main
