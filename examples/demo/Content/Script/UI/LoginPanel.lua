local LoginPanel = {}

-- 磁盘上只有 T_Cloth_0 .. T_Cloth_3，没有 T_Cloth 本身。
-- 这是引擎把 FName 的编号后缀剥离导致的，不是死引用。
local CLOTH_PREFIX = "/Game/Art/UI/T_Cloth"

function LoginPanel.onOpen()
    UE.UObject.Load(CLOTH_PREFIX)
    UE.UObject.Load("/Game/Art/UI/T_LoginBg")
    UE.UObject.Load("/Game/Art/UI/WBP_Login_C")
end

function LoginPanel.onRefresh()
    -- 已改名：现在是 NewHero_Body
    UE.UObject.Load("/Game/Character/OldHero_Body")
end

return LoginPanel
