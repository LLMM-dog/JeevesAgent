# jeeves 启动脚本（Windows）。
#
# 默认起开发模式：后端 --reload + 前端 vite dev server。
# 加 -Prod 则先构建前端再只起后端（后端会伺服静态文件）。
#
# 用法：
#   .\start.ps1              开发模式
#   .\start.ps1 -Prod        生产模式
#   .\start.ps1 -BackendOnly 只起后端

param(
    [switch]$Dev,        # start.bat 传 -Dev。默认就是开发模式，此参数只为兼容 start.bat 的调用方式
    [switch]$Prod,
    [switch]$BackendOnly,
    [int]$Port = 9000
)

$ErrorActionPreference = "Stop"

# start.bat 先执行了 `chcp 65001`，但 Windows 自带的 PowerShell 5.1
# 不会同步更新 [Console]::OutputEncoding（仍是系统 ANSI 代码页）。
# 结果 Write-Host 按 GBK 编码中文、控制台按 UTF-8 解码，每个中文字符
# 被拆错位后逐字重复（"启动后端" 显示成 "启启动动后后端端"）。
# 这里手动对齐输出编码。
try {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $enc
    [Console]::InputEncoding = $enc
    $OutputEncoding = $enc
} catch {
    # 没有控制台（重定向/管道）时忽略 —— 此时编码由重定向目标决定
}

$root = Split-Path $PSScriptRoot -Parent

function Fail($msg) {
    Write-Host "✗ $msg" -ForegroundColor Red
    exit 1
}

function Stop-Tree {
    <#
    .SYNOPSIS
    递归杀掉整棵进程树（先子后父）。

    .DESCRIPTION
    进程树实际是三层：powershell → uv → uvicorn。

    只杀 uv 的话 uvicorn 会变成孤儿并继续占端口 —— 下次启动报
    "端口被占用"，而用户以为是别的程序占了。

    必须先杀子再杀父：反过来的话父进程一死，子进程的 ParentProcessId
    就指向一个不存在的 PID，再也查不到它们。
    #>
    param([int]$ProcessId)

    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Tree -ProcessId $_.ProcessId }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

# ── 前置检查 ──
#
# 这三项任何一项缺失，启动都会失败，而 uvicorn 的报错通常不指向真因。
# 提前检查并给出可执行的下一步。

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "找不到 uv。安装：powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Fail "缺少 .env。先跑：python scripts\setup.py"
}

# ENCRYPTION_KEY 为空时后端会拒绝启动，且报错信息是
# "ENCRYPTION_KEY 缺失，拒绝启动" —— 虽然明确，但用户不知道怎么生成。
$envTxt = Get-Content (Join-Path $root ".env") -Raw
if ($envTxt -notmatch "JEEVES_SECURITY__ENCRYPTION_KEY=\S") {
    Fail "'.env' 里 JEEVES_SECURITY__ENCRYPTION_KEY 为空。跑 python scripts\setup.py 自动生成"
}

# ── 端口抢占检查 ──
#
# 如果上次 start.ps1 被强制 kill（任务管理器、意外重启），finally 块
# 不会执行，uvicorn 进程会变成孤儿并继续占着端口。
# 下次启动时 uvicorn 直接报 "Address already in use" 然后退出，
# 而 start.ps1 会说"后端启动失败"，完全不指向"端口被占"这个真因。
#
# 这里提前检查并询问是否清理。

$occupant = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($occupant) {
    $ci = Get-CimInstance Win32_Process -Filter "ProcessId=$($occupant.OwningProcess)" -ErrorAction SilentlyContinue
    Write-Host "⚠ 端口 $Port 已被占用（PID $($occupant.OwningProcess) $($ci.Name)）" -ForegroundColor Yellow
    Write-Host "  可能是上次没有正常退出留下的进程。" -ForegroundColor DarkGray
    $ans = Read-Host "  清理并继续？[Y/n]"
    if ($ans -eq "" -or $ans -imatch "^y") {
        Stop-Tree -ProcessId $occupant.OwningProcess
        Start-Sleep -Seconds 1
        $check = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($check) {
            Fail "清理后端口仍被占用（PID $($check.OwningProcess)），请手动关掉它再试"
        }
        Write-Host "  ✓ 已清理" -ForegroundColor Green
    } else {
        Fail "端口被占用，已取消"
    }
}

# ── 生产模式：先构建前端 ──

if ($Prod) {
    if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
        Fail "前端依赖未安装。先跑：cd frontend; npm install"
    }

    # 【只在源码比产物新时才构建】。
    #
    # 生产模式是普通用户的默认入口（start.bat 双击），而无条件构建
    # 意味着每次启动都白等 7 秒 —— 用户什么都没改，产物却重新生成一遍。
    #
    # 判据是"最新的源文件 vs dist/index.html 的时间戳"。
    # 比较 index.html 而不是整个 dist 目录：目录的修改时间在某些
    # 文件系统上不随内容更新，而 index.html 每次构建必然重写。
    #
    # 覆盖的源：src/ 下所有文件，加上会影响构建结果的配置
    # （vite.config、tsconfig、package.json、index.html、tailwind 配置）。
    # 漏掉配置文件的话，改了 vite.config 却不会重新构建 ——
    # 那种"改了没生效"最难排查。
    $dist = Join-Path $root "frontend\dist\index.html"
    $needBuild = $true
    if (Test-Path $dist) {
        $distTime = (Get-Item $dist).LastWriteTimeUtc
        $srcRoot = Join-Path $root "frontend"
        $watch = @(Get-ChildItem (Join-Path $srcRoot "src") -Recurse -File -ErrorAction SilentlyContinue)
        foreach ($n in @("vite.config.ts", "tsconfig.json", "tsconfig.app.json",
                         "package.json", "index.html", "tailwind.config.ts",
                         "postcss.config.js")) {
            $f = Join-Path $srcRoot $n
            if (Test-Path $f) { $watch += Get-Item $f }
        }
        $newest = $watch | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if ($newest -and $newest.LastWriteTimeUtc -le $distTime) {
            $needBuild = $false
            Write-Host "前端产物已是最新，跳过构建" -ForegroundColor DarkGray
            Write-Host "  （要强制重建：cd frontend; npm run build）" -ForegroundColor DarkGray
        }
    }

    if ($needBuild) {
        Write-Host "构建前端…（首次或源码有改动，约 5-10 秒）" -ForegroundColor Cyan
        Push-Location (Join-Path $root "frontend")
        try {
            npm run build
            if ($LASTEXITCODE -ne 0) { Fail "前端构建失败" }
        } finally {
            Pop-Location
        }
        Write-Host "✓ 构建完成" -ForegroundColor Green
    }
}

# ── 起后端 ──

$backendArgs = @(
    "run", "uvicorn", "app.main:app",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--app-dir", "backend"
)
# 生产模式不要 --reload：它会多起一个 watch 进程，
# 而文件变化时重启会中断正在进行的对话流
if (-not $Prod) {
    $backendArgs += "--reload"
}

if ($Prod -or $BackendOnly) {
    Write-Host "启动后端 http://127.0.0.1:$Port" -ForegroundColor Cyan
    if ($Prod) {
        Write-Host "（前端已构建，直接访问上面这个地址）" -ForegroundColor DarkGray
    }
    # 放后台然后等就绪，这样可以在启动后自动打开浏览器
    $proc = Start-Process -FilePath "uv" -ArgumentList $backendArgs -PassThru -NoNewWindow
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 300
        if ($proc.HasExited) {
            Write-Host "✗ 后端启动失败" -ForegroundColor Red
            exit $proc.ExitCode
        }
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 -UseBasicParsing
            if ($r.StatusCode -eq 200) { break }
        } catch { }
    }
    Start-Process "http://127.0.0.1:$Port"
    $proc.WaitForExit()
    exit $proc.ExitCode
}

# ── 开发模式：两个都起 ──
#
# 后端放后台，前端放前台。
#
# 为什么这样分：前端 vite 的输出（HMR 提示、编译错误）是开发时最常看的，
# 放前台方便。而后端日志需要时再看文件。
#
# 反过来（后端前台）的话，Ctrl-C 只停后端，前端会变成孤儿进程继续占端口 ——
# 下次启动报"端口被占用"，而用户不知道是上次没退干净。

if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
    Fail "前端依赖未安装。先跑：cd frontend; npm install"
}

$logDir = Join-Path $root "data"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$backendLog = Join-Path $logDir "backend.log"

Write-Host "启动后端（日志：data\backend.log）…" -ForegroundColor Cyan
$backend = Start-Process -FilePath "uv" -ArgumentList $backendArgs `
    -WorkingDirectory $root -PassThru -NoNewWindow `
    -RedirectStandardOutput $backendLog -RedirectStandardError "$backendLog.err"

# 等后端起来。不等的话前端第一次请求会失败，
# 浏览器控制台一片红，而实际只是启动竞态。
$ready = $false
foreach ($i in 1..40) {
    Start-Sleep -Milliseconds 500
    if ($backend.HasExited) {
        Write-Host "✗ 后端启动失败，日志尾部：" -ForegroundColor Red
        if (Test-Path "$backendLog.err") { Get-Content "$backendLog.err" -Tail 20 }
        if (Test-Path $backendLog) { Get-Content $backendLog -Tail 20 }
        exit 1
    }
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # 还没起来，继续等
    }
}

if ($ready) {
    Write-Host "✓ 后端就绪 http://127.0.0.1:$Port" -ForegroundColor Green
    # 生产模式下自动打开浏览器
    if ($Prod) { Start-Process "http://127.0.0.1:$Port" }
} else {
    Write-Host "⚠ 后端 20 秒内没响应健康检查，但进程还在。继续起前端" -ForegroundColor Yellow
}

Write-Host "启动前端…（Ctrl-C 停止，会一并停掉后端）" -ForegroundColor Cyan
Write-Host ""

try {
    Push-Location (Join-Path $root "frontend")
    npm run dev
} finally {
    Pop-Location
    # 【必须清理后端】。
    #
    # 不清的话 Ctrl-C 后后端还占着 9000，下次启动报"端口被占用"，
    # 而用户以为是别的程序占了。
    if ($backend -and -not $backend.HasExited) {
        Write-Host ""
        Write-Host "停止后端…" -ForegroundColor DarkGray
        Stop-Tree -ProcessId $backend.Id
    }

    # 【必须按端口找到监听者并杀它的整棵树】。
    #
    # 进程树实测是：powershell → uv（$backend.Id）→ python（venv 入口）→ uvicorn（python）
    #
    # 问题：uvicorn 进程的【父进程不是 uv，而是 venv 的 python 包装器】——
    # 它在 uv 的子树里，但走的是另一条路：
    #   uv 启动 → 启动 venv/python 入口 → 再启动 uvicorn（父 PID = venv python）
    # 所以从 uv PID 出发的 Stop-Tree 有时抓不到 uvicorn。
    #
    # 兜底：找端口 $Port 的实际监听者，对它整棵树再做一遍 Stop-Tree。
    # 代价是多一次 WMI 查询（几十毫秒），而漏杀 uvicorn 的代价是
    # 下次启动报"端口被占用"，而用户以为是别的程序占了。
    Start-Sleep -Milliseconds 500
    $stuck = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($stuck) {
        Write-Host "清理残留的端口占用…" -ForegroundColor DarkGray
        $stuck | ForEach-Object { Stop-Tree -ProcessId $_.OwningProcess }
        Start-Sleep -Milliseconds 800
    }
}
