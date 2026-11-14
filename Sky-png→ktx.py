#!/usr/bin/env python3
"""
全格式纹理转换器 - Texture Converter
======================================
图片 ↔ KTX 格式双向转换工具

功能模式:
  简易模式   - 快速 PNG→KTX 转换（原 Sky-png→ktx.py 功能）
  专家模式   - 全格式选择转换（ETC2/EAC、BC7/BPTC、S3TC/BC1-BC5、ASTC、未压缩）
  参考模式   - 从现有 KTX 文件读取格式，用相同格式转换新图片
  KTX→图片   - 将任意格式的 KTX 解压为通用图片（PNG/JPG/BMP/WebP/TIFF/TGA）

支持输入格式:
  PNG, JPG, JPEG, JFIF, BMP, WebP, TIFF, TIF, GIF, ICO, TGA, ICNS, EXR, HDR, PSD

支持输出格式:
  ETC2 / EAC 系列 (OpenGL ES 3.0 标准，所有安卓设备支持)
  BC7 / BPTC 系列 (高端纹理压缩，DX11+ 和高端安卓支持)
  S3TC / BC1-BC5 系列 (桌面显卡标准压缩 DXT1-5)
  ASTC 系列 (自适应纹理压缩，现代移动设备支持)
  未压缩格式 (原始像素数据)

依赖管理:
  自动检测依赖状态
  可选安装缺失依赖（选项 [6]）
  安装失败时提供手动安装命令（选项 [7]）
"""

import argparse
import struct
import sys
import subprocess
import os
import tempfile
import shutil
from pathlib import Path


# ============================================================
#  依赖检测系统
# ============================================================

# 全局依赖状态字典
DEPENDENCY_STATUS = {
    'pillow': {
        'available': False,
        'package': 'Pillow',
        'desc': 'Pillow 图片处理库',
        'purpose': '读取各种格式的图片文件',
    },
    'numpy': {
        'available': False,
        'package': 'numpy',
        'desc': 'NumPy 数值计算库',
        'purpose': '像素数据处理和透明通道检测',
    },
    'etcpak': {
        'available': False,
        'package': 'etcpak',
        'desc': 'etcpak 压缩库',
        'purpose': 'ETC2、EAC、BC7 格式的纹理压缩',
    },
    'archspec': {
        'available': False,
        'package': 'archspec',
        'desc': 'archspec 架构检测',
        'purpose': 'etcpak 的依赖，用于检测 CPU 架构',
    },
    'astcenc': {
        'available': False,
        'package': 'astc-encoder',
        'desc': 'ASTC 编码器',
        'purpose': 'ASTC 格式的纹理压缩与解压',
        'type': 'system',  # 系统级工具，非 Python 包
    },
    'squish': {
        'available': False,
        'package': 'squish',
        'desc': 'Squish 压缩库',
        'purpose': 'S3TC/BC1-BC5 格式的纹理压缩 (DXT1-5)',
    },
    'texture2ddecoder': {
        'available': False,
        'package': 'texture2ddecoder',
        'desc': 'texture2ddecoder 纹理解码库',
        'purpose': 'ETC2、BC1-BC7 格式的纹理解码（KTX→图片 所需）',
    },
}

# 支持的输入图片格式（映射文件扩展名到格式名称）
SUPPORTED_INPUT_FORMATS = {
    '.png':  'PNG',
    '.jpg':  'JPEG',
    '.jpeg': 'JPEG',
    '.jfif': 'JPEG',
    '.bmp':  'BMP',
    '.webp': 'WebP',
    '.tiff': 'TIFF',
    '.tif':  'TIFF',
    '.gif':  'GIF',
    '.ico':  'ICO',
    '.tga':  'TGA',
    '.icns': 'ICNS',
    '.exr':  'EXR',
    '.hdr':  'HDR',
    '.psd':  'PSD',
}

# 支持的输出图片格式（KTX→图片）
SUPPORTED_OUTPUT_IMAGE_FORMATS = {
    '.png':  'PNG',
    '.jpg':  'JPEG',
    '.jpeg': 'JPEG',
    '.bmp':  'BMP',
    '.webp': 'WebP',
    '.tiff': 'TIFF',
    '.tif':  'TIFF',
    '.tga':  'TGA',
}


def detect_dependencies():
    """
    检测所有依赖的可用性
    尝试导入每个 Python 模块，或检查系统命令是否存在
    结果写入全局 DEPENDENCY_STATUS 字典
    """
    # ---- Pillow (PIL) ----
    try:
        from PIL import Image
        DEPENDENCY_STATUS['pillow']['available'] = True
    except ImportError:
        pass

    # ---- NumPy ----
    try:
        import numpy as np
        DEPENDENCY_STATUS['numpy']['available'] = True
    except ImportError:
        pass

    # ---- etcpak ----
    try:
        import etcpak
        DEPENDENCY_STATUS['etcpak']['available'] = True
    except ImportError:
        pass

    # ---- archspec ----
    try:
        import archspec
        DEPENDENCY_STATUS['archspec']['available'] = True
    except ImportError:
        pass

    # ---- ASTC 编码器 (系统命令) ----
    DEPENDENCY_STATUS['astcenc']['available'] = shutil.which('astcenc') is not None

    # ---- Squish ----
    try:
        import squish
        DEPENDENCY_STATUS['squish']['available'] = True
    except ImportError:
        pass

    # ---- texture2ddecoder ----
    try:
        import texture2ddecoder
        DEPENDENCY_STATUS['texture2ddecoder']['available'] = True
    except ImportError:
        pass

    # 更新全局便捷变量
    global SQUISH_AVAILABLE, ASTC_AVAILABLE, TEXTURE2DDECODER_AVAILABLE
    SQUISH_AVAILABLE = DEPENDENCY_STATUS['squish']['available']
    ASTC_AVAILABLE = DEPENDENCY_STATUS['astcenc']['available']
    TEXTURE2DDECODER_AVAILABLE = DEPENDENCY_STATUS['texture2ddecoder']['available']


# 立即执行一次检测
detect_dependencies()

# 尝试导入核心模块（可能失败，安装依赖后会成功）
try:
    import numpy as np
    from PIL import Image
    import etcpak
except ImportError:
    pass  # 静默处理，后续使用时会检查

# Squish 可选
try:
    import squish
    SQUISH_AVAILABLE = True
except ImportError:
    SQUISH_AVAILABLE = False

# ASTC 编码器状态
ASTC_AVAILABLE = DEPENDENCY_STATUS['astcenc']['available']

# texture2ddecoder 可选（用于 KTX→图片）
try:
    import texture2ddecoder
    TEXTURE2DDECODER_AVAILABLE = True
except ImportError:
    TEXTURE2DDECODER_AVAILABLE = False


# ============================================================
#  依赖管理函数
# ============================================================

def get_missing_dependencies():
    """
    获取当前缺失的依赖列表
    返回: [(名称, 信息字典), ...]
    """
    missing = []
    for name, info in DEPENDENCY_STATUS.items():
        if not info['available']:
            missing.append((name, info))
    return missing


def show_dependency_status(compact=True):
    """
    显示依赖状态
    Args:
        compact: True 时显示简要信息，False 时显示详细列表
    """
    if compact:
        missing = [info['desc'] for name, info in get_missing_dependencies()]
        if missing:
            print(f"⚠️  缺失依赖: {', '.join(missing)}")
            print("   → 选择选项 [6] 自动安装，或 [7] 查看手动命令")
        else:
            print("✅ 所有依赖已就绪")
    else:
        # 详细状态
        print("\n" + "=" * 60)
        print("📋 依赖状态详情")
        print("=" * 60)
        for name, info in DEPENDENCY_STATUS.items():
            status_icon = "✅" if info['available'] else "❌"
            print(f"  {status_icon} {info['desc']}")
            print(f"     用途: {info['purpose']}")
            if not info['available']:
                if info.get('type') == 'system':
                    print(f"     安装: pkg install {info['package']}")
                else:
                    print(f"     安装: pip install {info['package']}")
            print()


def install_pip_package(package, description=None):
    """
    使用 pip 安装 Python 包
    Args:
        package: 包名
        description: 显示名称
    Returns:
        bool: 安装是否成功
    """
    desc = description or package
    try:
        print(f"   📦 正在安装 {desc}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"   ✅ {desc} 安装成功")
        return True
    except subprocess.CalledProcessError:
        print(f"   ❌ {desc} 安装失败")
        return False


def install_system_package(package, description=None):
    """
    使用 pkg 安装系统包（Termux 环境）
    Args:
        package: 包名
        description: 显示名称
    Returns:
        bool: 安装是否成功
    """
    desc = description or package

    # 检查是否在 Termux 环境
    if not shutil.which('pkg'):
        print(f"   ⚠️  非 Termux 环境，无法使用 pkg 安装 {desc}")
        return False

    try:
        print(f"   📦 正在安装系统包 {desc}...")
        subprocess.check_call(
            ["pkg", "install", "-y", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"   ✅ {desc} 安装成功")
        return True
    except subprocess.CalledProcessError:
        print(f"   ❌ {desc} 安装失败")
        return False


def install_astcenc():
    """
    安装 ASTC 编码器
    方法1: pkg install astc-encoder (Termux)
    方法2: 从 GitHub 下载预编译版本
    Returns:
        bool: 安装是否成功
    """
    # 已经可用，无需安装
    if DEPENDENCY_STATUS['astcenc']['available']:
        return True

    print("\n   🔧 正在安装 ASTC 编码器...")

    # ---- 方法 1: Termux 包管理器 ----
    print("   方法 1: pkg install astc-encoder")
    if install_system_package('astc-encoder', 'ASTC 编码器'):
        if shutil.which('astcenc'):
            DEPENDENCY_STATUS['astcenc']['available'] = True
            return True

    # ---- 方法 2: GitHub 下载预编译版本 ----
    print("   方法 2: 从 GitHub 下载预编译版本")
    try:
        import urllib.request
        import platform

        # 架构映射
        arch_map = {
            'aarch64': 'aarch64',
            'arm64': 'aarch64',
            'armv7l': 'arm',
            'arm': 'arm',
            'x86_64': 'x86_64',
        }
        arch = arch_map.get(platform.machine())
        if not arch:
            raise Exception(f"不支持的 CPU 架构: {platform.machine()}")

        # 版本和文件名
        version = "4.8.0"
        filename = f"astcenc-{version}-linux-{arch}.zip"
        url = f"https://github.com/ARM-software/astc-encoder/releases/download/{version}/{filename}"

        # 下载目录
        download_dir = Path.home() / '.local' / 'bin'
        download_dir.mkdir(parents=True, exist_ok=True)

        zip_path = download_dir / filename
        print(f"   📥 下载 {filename}...")
        urllib.request.urlretrieve(url, zip_path)

        # 解压
        print("   📦 解压中...")
        import zipfile
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(download_dir)

        # 查找 astcenc 可执行文件
        astcenc_path = None
        for root, dirs, files in os.walk(download_dir):
            for f in files:
                if f == 'astcenc' or f.startswith('astcenc'):
                    astcenc_path = Path(root) / f
                    break
            if astcenc_path:
                break

        if astcenc_path:
            # 设置可执行权限
            astcenc_path.chmod(0o755)

            # 创建符号链接
            target_link = download_dir / 'astcenc'
            if target_link.exists():
                target_link.unlink()
            target_link.symlink_to(astcenc_path)

            # 添加到 PATH（如果不在）
            bin_dir = str(download_dir)
            if bin_dir not in os.environ.get('PATH', ''):
                shell_rc = Path.home() / '.bashrc'
                with open(shell_rc, 'a') as f:
                    f.write(f'\nexport PATH="{bin_dir}:$PATH"\n')
                print(f"   ℹ️  已将 {bin_dir} 添加到 PATH，重启终端生效")

            # 清理下载文件
            os.remove(zip_path)

            DEPENDENCY_STATUS['astcenc']['available'] = True
            print(f"   ✅ ASTC 编码器安装成功")
            return True

        os.remove(zip_path)

    except Exception as e:
        print(f"   ❌ 下载安装失败: {e}")

    return False


def install_squish():
    """
    安装 S3TC 压缩库（Squish）
    方法1: pip install squish
    方法2: 从 GitHub 源码编译安装
    Returns:
        bool: 安装是否成功
    """
    # 已经可用
    if DEPENDENCY_STATUS['squish']['available']:
        return True

    print("\n   🔧 正在安装 S3TC 压缩库...")

    # ---- 方法 1: pip 直接安装 ----
    print("   方法 1: pip install squish")
    if install_pip_package('squish', 'Squish 库'):
        try:
            import squish
            DEPENDENCY_STATUS['squish']['available'] = True
            return True
        except ImportError:
            pass

    # ---- 方法 2: 从 GitHub 源码安装 ----
    print("   方法 2: 从 GitHub 源码编译安装")
    try:
        import urllib.request

        # 创建临时目录
        temp_dir = tempfile.mkdtemp()
        url = "https://github.com/nickwn/squish-python/archive/refs/heads/master.zip"
        zip_path = os.path.join(temp_dir, 'squish.zip')

        print("   📥 下载源码...")
        urllib.request.urlretrieve(url, zip_path)

        # 解压
        import zipfile
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(temp_dir)

        # 查找 setup.py
        squish_dir = os.path.join(temp_dir, 'squish-python-master')
        if os.path.exists(os.path.join(squish_dir, 'setup.py')):
            print("   🔨 编译安装中...")
            subprocess.check_call(
                [sys.executable, "setup.py", "install"],
                cwd=squish_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # 验证安装
            import squish
            DEPENDENCY_STATUS['squish']['available'] = True
            print("   ✅ Squish 安装成功")
            return True
        else:
            print("   ❌ 未找到 setup.py")

    except Exception as e:
        print(f"   ❌ 源码安装失败: {e}")

    return False


def install_all_dependencies():
    """
    安装所有缺失的依赖
    按顺序安装：基础库 → etcpak → ASTC → S3TC → texture2ddecoder
    """
    print()
    print("=" * 60)
    print("📦 自动安装缺失依赖")
    print("=" * 60)
    print()

    # ---- 1. Pillow ----
    if not DEPENDENCY_STATUS['pillow']['available']:
        print("📦 [1/7] 安装 Pillow (图片处理库)")
        if install_pip_package('Pillow', 'Pillow'):
            DEPENDENCY_STATUS['pillow']['available'] = True
        else:
            print("   ⚠️  Pillow 安装失败，请手动: pip install Pillow")

    # ---- 2. NumPy ----
    if not DEPENDENCY_STATUS['numpy']['available']:
        print("📦 [2/7] 安装 NumPy (数值计算库)")
        if install_pip_package('numpy', 'NumPy'):
            DEPENDENCY_STATUS['numpy']['available'] = True
        else:
            print("   ⚠️  NumPy 安装失败，请手动: pip install numpy")

    # ---- 3. archspec ----
    if not DEPENDENCY_STATUS['archspec']['available']:
        print("📦 [3/7] 安装 archspec (架构检测)")
        if install_pip_package('archspec', 'archspec'):
            DEPENDENCY_STATUS['archspec']['available'] = True
        else:
            print("   ⚠️  archspec 安装失败，请手动: pip install archspec")

    # ---- 4. etcpak ----
    if not DEPENDENCY_STATUS['etcpak']['available']:
        print("📦 [4/7] 安装 etcpak (ETC2/BC7 压缩库)")
        if install_pip_package('etcpak', 'etcpak'):
            DEPENDENCY_STATUS['etcpak']['available'] = True
        else:
            print("   ⚠️  etcpak 安装失败，请手动: pip install etcpak")

    # ---- 5. ASTC 编码器 ----
    if not DEPENDENCY_STATUS['astcenc']['available']:
        print("📦 [5/7] 安装 ASTC 编码器")
        if not install_astcenc():
            print("   ⚠️  ASTC 编码器安装失败")
            print("   手动安装:")
            print("     pkg install astc-encoder")
            print("     或访问: https://github.com/ARM-software/astc-encoder/releases")

    # ---- 6. Squish (S3TC) ----
    if not DEPENDENCY_STATUS['squish']['available']:
        print("📦 [6/7] 安装 Squish (S3TC 压缩库)")
        if not install_squish():
            print("   ⚠️  Squish 安装失败")
            print("   手动安装: pip install squish")

    # ---- 7. texture2ddecoder (KTX→图片) ----
    if not DEPENDENCY_STATUS['texture2ddecoder']['available']:
        print("📦 [7/7] 安装 texture2ddecoder (纹理解码库，KTX→图片所需)")
        if install_pip_package('texture2ddecoder', 'texture2ddecoder'):
            DEPENDENCY_STATUS['texture2ddecoder']['available'] = True
        else:
            print("   ⚠️  texture2ddecoder 安装失败")
            print("   手动安装: pip install texture2ddecoder")

    # 重新检测依赖状态
    detect_dependencies()

    # 显示结果
    print()
    print("=" * 60)
    print("📋 安装结果")
    print("=" * 60)
    show_dependency_status(compact=False)


def print_manual_install_commands():
    """
    打印所有缺失依赖的手动安装命令
    当自动安装失败时，用户可以参考这些命令
    """
    missing = get_missing_dependencies()

    if not missing:
        print("✅ 所有依赖已就绪，无需手动安装")
        return

    print()
    print("=" * 60)
    print("📋 手动安装命令")
    print("=" * 60)
    print()
    print("以下命令可以帮助你手动安装缺失的依赖：")
    print()

    # 收集所有需要 pip 安装的包
    pip_packages = []
    for name, info in missing:
        if info.get('type') != 'system':
            pip_packages.append(info['package'])

    if pip_packages:
        print(f"📦 Python 包 (pip 安装):")
        print(f"   pip install {' '.join(pip_packages)}")
        print()

    # 逐个给出具体命令
    for name, info in missing:
        if name == 'astcenc':
            print(f"📦 {info['desc']}:")
            print(f"   用途: {info['purpose']}")
            if shutil.which('pkg'):
                print(f"   Termux: pkg install astc-encoder")
            print(f"   手动下载: https://github.com/ARM-software/astc-encoder/releases")
            print(f"   下载后解压，将 astcenc 放入 PATH 目录")
            print()
        elif name == 'squish':
            print(f"📦 {info['desc']}:")
            print(f"   用途: {info['purpose']}")
            print(f"   方法1: pip install squish")
            print(f"   方法2: pip install git+https://github.com/nickwn/squish-python.git")
            print()
        elif name == 'texture2ddecoder':
            print(f"📦 {info['desc']}:")
            print(f"   用途: {info['purpose']}")
            print(f"   pip install texture2ddecoder")
            print(f"   (KTX→图片功能中，BC/ETC 格式解压必需)")
            print()

    # Termux 环境额外提示
    if shutil.which('pkg'):
        print("📦 Termux 编译工具 (如果上述方法需要编译):")
        print("   pkg install python build-essential cmake ninja clang")
        print()

    print("安装完成后，重新运行脚本即可。")
    print("=" * 60)


# ============================================================
#  格式定义
# ============================================================

class TextureFormat:
    """
    纹理格式定义

    属性:
        gl_code: OpenGL 内部格式代码
        name: 格式名称
        category: 格式类别 (etc2/bc7/s3tc/astc/uncompressed)
        channels: 通道描述
        default: 是否为该类别的默认格式
        requires_s3tc: 是否需要 Squish 库
        requires_astc: 是否需要 ASTC 编码器
        requires_etcpak: 是否需要 etcpak 库
        requires_decoder: 是否需要 texture2ddecoder 库（KTX→图片解压）
        block_size: ASTC 块大小 (宽, 高)
    """
    def __init__(self, gl_code, name, category, channels, default=False,
                 requires_s3tc=False, requires_astc=False, requires_etcpak=False,
                 requires_decoder=False, block_size=None):
        self.gl_code = gl_code
        self.name = name
        self.category = category
        self.channels = channels
        self.default = default
        self.requires_s3tc = requires_s3tc
        self.requires_astc = requires_astc
        self.requires_etcpak = requires_etcpak
        self.requires_decoder = requires_decoder
        self.block_size = block_size

    @property
    def is_available(self):
        """
        检查该格式当前是否可用（用于压缩/编码场景）
        根据所需的依赖是否安装来判断
        """
        if self.requires_astc and not ASTC_AVAILABLE:
            return False
        if self.requires_s3tc and not SQUISH_AVAILABLE:
            return False
        if self.requires_etcpak and not DEPENDENCY_STATUS['etcpak']['available']:
            return False
        return True

    @property
    def is_decodable(self):
        """
        检查该格式当前是否可解码（用于 KTX→图片 场景）
        """
        if self.category == 'uncompressed':
            return True
        if self.category == 'astc':
            return ASTC_AVAILABLE
        if self.category in ('etc2', 'bc7', 's3tc'):
            return TEXTURE2DDECODER_AVAILABLE
        return False


# ============================================================
#  所有支持的纹理格式
# ============================================================

FORMATS = {
    # =================== ETC2 / EAC 系列 ===================
    # OpenGL ES 3.0 标准格式，所有安卓设备必须支持
    'etc2_r11':           TextureFormat(
        0x9270, 'ETC2_R11_EAC', 'etc2', '单通道(R)',
        requires_etcpak=True, requires_decoder=True),
    'etc2_r11_signed':    TextureFormat(
        0x9271, 'ETC2_SIGNED_R11_EAC', 'etc2', '单通道(R有符号)',
        requires_etcpak=True, requires_decoder=True),
    'etc2_rg11':          TextureFormat(
        0x9272, 'ETC2_RG11_EAC', 'etc2', '双通道(RG)',
        requires_etcpak=True, requires_decoder=True),
    'etc2_rg11_signed':   TextureFormat(
        0x9273, 'ETC2_SIGNED_RG11_EAC', 'etc2', '双通道(RG有符号)',
        requires_etcpak=True, requires_decoder=True),
    'etc2_rgb8':          TextureFormat(
        0x9274, 'ETC2_RGB8', 'etc2', 'RGB',
        default=True, requires_etcpak=True, requires_decoder=True),
    'etc2_srgb8':         TextureFormat(
        0x9275, 'ETC2_SRGB8', 'etc2', 'RGB (sRGB色彩空间)',
        requires_etcpak=True, requires_decoder=True),
    'etc2_rgb8a1':        TextureFormat(
        0x9276, 'ETC2_RGB8A1', 'etc2', 'RGB + 1位Alpha',
        requires_etcpak=True, requires_decoder=True),
    'etc2_srgb8a1':       TextureFormat(
        0x9277, 'ETC2_SRGB8A1', 'etc2', 'RGB (sRGB) + 1位Alpha',
        requires_etcpak=True, requires_decoder=True),
    'etc2_rgba8':         TextureFormat(
        0x9278, 'ETC2_RGBA8_EAC', 'etc2', 'RGBA',
        default=True, requires_etcpak=True, requires_decoder=True),
    'etc2_srgb8_alpha8':  TextureFormat(
        0x9279, 'ETC2_SRGB8_ALPHA8_EAC', 'etc2', 'RGBA (sRGB色彩空间)',
        requires_etcpak=True, requires_decoder=True),
    'etc1_rgb8':          TextureFormat(
        0x8D64, 'ETC1_RGB8', 'etc2', 'RGB',
        requires_etcpak=True, requires_decoder=True),

    # =================== BC7 / BPTC 系列 ===================
    # 高端纹理压缩格式，DX11+ 和高性能安卓设备支持
    'bc7_srgb_alpha':     TextureFormat(
        0x8C43, 'BC7_SRGB_ALPHA', 'bc7', 'RGBA (sRGB色彩空间)',
        requires_etcpak=True, requires_decoder=True),
    'bc7_unorm_alpha':    TextureFormat(
        0x8C44, 'BC7_UNORM_ALPHA', 'bc7', 'RGBA',
        requires_etcpak=True, requires_decoder=True),
    'bc7_srgb_alpha2':    TextureFormat(
        0x8C45, 'BC7_SRGB_ALPHA', 'bc7', 'RGBA (sRGB色彩空间)',
        requires_etcpak=True, requires_decoder=True),
    'bc7_unorm_alpha2':   TextureFormat(
        0x8C46, 'BC7_UNORM_ALPHA', 'bc7', 'RGBA',
        requires_etcpak=True, requires_decoder=True),
    'bc7_srgb':           TextureFormat(
        0x8C47, 'BC7_SRGB', 'bc7', 'RGB (sRGB色彩空间)',
        default=True, requires_etcpak=True, requires_decoder=True),
    'bc7_unorm':          TextureFormat(
        0x8C48, 'BC7_UNORM', 'bc7', 'RGB',
        default=True, requires_etcpak=True, requires_decoder=True),
    'bc7_srgb2':          TextureFormat(
        0x8C49, 'BC7_SRGB', 'bc7', 'RGB (sRGB色彩空间)',
        requires_etcpak=True, requires_decoder=True),
    'bc7_unorm2':         TextureFormat(
        0x8C4A, 'BC7_UNORM', 'bc7', 'RGB',
        requires_etcpak=True, requires_decoder=True),

    # =================== S3TC / BC1-BC5 系列 ===================
    # 桌面显卡标准格式 (DXT1-DXT5)
    'bc1_rgb':            TextureFormat(
        0x83F0, 'BC1_RGB (DXT1)', 's3tc', 'RGB',
        requires_s3tc=True, requires_decoder=True),
    'bc1_rgba':           TextureFormat(
        0x83F1, 'BC1_RGBA (DXT1)', 's3tc', 'RGBA (1位Alpha)',
        requires_s3tc=True, requires_decoder=True),
    'bc2_rgba':           TextureFormat(
        0x83F2, 'BC2_RGBA (DXT3)', 's3tc', 'RGBA',
        requires_s3tc=True, requires_decoder=True),
    'bc3_rgba':           TextureFormat(
        0x83F3, 'BC3_RGBA (DXT5)', 's3tc', 'RGBA',
        default=True, requires_s3tc=True, requires_decoder=True),

    # =================== ASTC 系列 ===================
    # 自适应可伸缩纹理压缩，现代移动设备支持
    'astc_4x4':           TextureFormat(
        0x93B0, 'ASTC_4x4', 'astc', 'RGBA',
        requires_astc=True, block_size=(4, 4)),
    'astc_5x4':           TextureFormat(
        0x93B1, 'ASTC_5x4', 'astc', 'RGBA',
        requires_astc=True, block_size=(5, 4)),
    'astc_5x5':           TextureFormat(
        0x93B2, 'ASTC_5x5', 'astc', 'RGBA',
        requires_astc=True, block_size=(5, 5)),
    'astc_6x5':           TextureFormat(
        0x93B3, 'ASTC_6x5', 'astc', 'RGBA',
        requires_astc=True, block_size=(6, 5)),
    'astc_6x6':           TextureFormat(
        0x93B4, 'ASTC_6x6', 'astc', 'RGBA',
        default=True, requires_astc=True, block_size=(6, 6)),
    'astc_8x5':           TextureFormat(
        0x93B5, 'ASTC_8x5', 'astc', 'RGBA',
        requires_astc=True, block_size=(8, 5)),
    'astc_8x6':           TextureFormat(
        0x93B6, 'ASTC_8x6', 'astc', 'RGBA',
        requires_astc=True, block_size=(8, 6)),
    'astc_8x8':           TextureFormat(
        0x93B7, 'ASTC_8x8', 'astc', 'RGBA',
        requires_astc=True, block_size=(8, 8)),
    'astc_10x5':          TextureFormat(
        0x93B8, 'ASTC_10x5', 'astc', 'RGBA',
        requires_astc=True, block_size=(10, 5)),
    'astc_10x6':          TextureFormat(
        0x93B9, 'ASTC_10x6', 'astc', 'RGBA',
        requires_astc=True, block_size=(10, 6)),
    'astc_10x8':          TextureFormat(
        0x93BA, 'ASTC_10x8', 'astc', 'RGBA',
        requires_astc=True, block_size=(10, 8)),
    'astc_10x10':         TextureFormat(
        0x93BB, 'ASTC_10x10', 'astc', 'RGBA',
        requires_astc=True, block_size=(10, 10)),
    'astc_12x10':         TextureFormat(
        0x93BC, 'ASTC_12x10', 'astc', 'RGBA',
        requires_astc=True, block_size=(12, 10)),
    'astc_12x12':         TextureFormat(
        0x93BD, 'ASTC_12x12', 'astc', 'RGBA',
        requires_astc=True, block_size=(12, 12)),

    # =================== 未压缩格式 ===================
    'rgb8':               TextureFormat(
        0x1907, 'RGB8', 'uncompressed', 'RGB'),
    'rgba8':              TextureFormat(
        0x1908, 'RGBA8', 'uncompressed', 'RGBA',
        default=True),
}

# GL 内部格式代码 → 格式键 快速查找表
GL_CODE_TO_FORMAT = {
    fmt.gl_code: (key, fmt)
    for key, fmt in FORMATS.items()
}

# 格式分类（用于交互式菜单）
FORMAT_CATEGORIES = {
    'ETC2 / EAC': {
        'desc': 'OpenGL ES 3.0 标准，所有安卓设备支持',
        'keys': [k for k, v in FORMATS.items() if v.category == 'etc2'],
    },
    'BC7 / BPTC': {
        'desc': '高端纹理压缩，DX11+ 和高端安卓设备支持',
        'keys': [k for k, v in FORMATS.items() if v.category == 'bc7'],
    },
    'S3TC / BC1-BC5': {
        'desc': '桌面显卡标准 (DXT1-5)',
        'keys': [k for k, v in FORMATS.items() if v.category == 's3tc'],
    },
    'ASTC': {
        'desc': '自适应纹理压缩，现代移动设备支持',
        'keys': [k for k, v in FORMATS.items() if v.category == 'astc'],
    },
    '未压缩': {
        'desc': '原始像素数据，无压缩',
        'keys': [k for k, v in FORMATS.items() if v.category == 'uncompressed'],
    },
}


# ============================================================
#  KTX 文件格式读写
# ============================================================

# KTX 文件标识符（12字节，小端）
KTX_IDENTIFIER = bytes([
    0xAB, 0x4B, 0x54, 0x58,  # "KTX "
    0x20, 0x31, 0x31,        # " 11"
    0xBB, 0x0D, 0x0A, 0x1A, 0x0A
])


def read_ktx_header(filepath):
    """
    读取 KTX 文件头，提取纹理格式信息

    Args:
        filepath: KTX 文件路径

    Returns:
        dict: 包含格式信息的字典，失败返回 None
        {
            'gl_internal_format': OpenGL 内部格式代码,
            'gl_format': OpenGL 格式,
            'gl_base_internal_format': 基础内部格式,
            'width': 宽度,
            'height': 高度,
            'depth': 深度,
        }
    """
    try:
        with open(filepath, 'rb') as f:
            # 验证 KTX 标识符 (12 字节)
            identifier = f.read(12)
            if identifier != KTX_IDENTIFIER:
                print(f"❌ 不是有效的 KTX 文件: {filepath}")
                return None

            # 字节序标记 (4 字节)
            endianness = struct.unpack('<I', f.read(4))[0]
            if endianness != 0x04030201:
                print(f"❌ 不支持的字节序")
                return None

            # glType (4 字节)
            gl_type = struct.unpack('<I', f.read(4))[0]

            # glTypeSize (4 字节)
            gl_type_size = struct.unpack('<I', f.read(4))[0]

            # glFormat (4 字节)
            gl_format = struct.unpack('<I', f.read(4))[0]

            # glInternalFormat (4 字节) - 这是最关键的信息
            gl_internal_format = struct.unpack('<I', f.read(4))[0]

            # glBaseInternalFormat (4 字节)
            gl_base_internal_format = struct.unpack('<I', f.read(4))[0]

            # pixelWidth (4 字节)
            width = struct.unpack('<I', f.read(4))[0]

            # pixelHeight (4 字节)
            height = struct.unpack('<I', f.read(4))[0]

            # pixelDepth (4 字节)
            depth = struct.unpack('<I', f.read(4))[0]

            return {
                'gl_type': gl_type,
                'gl_type_size': gl_type_size,
                'gl_internal_format': gl_internal_format,
                'gl_format': gl_format,
                'gl_base_internal_format': gl_base_internal_format,
                'width': width,
                'height': height,
                'depth': depth,
            }

    except FileNotFoundError:
        print(f"❌ 文件不存在: {filepath}")
        return None
    except Exception as e:
        print(f"❌ 读取 KTX 文件失败: {e}")
        return None


def read_ktx_full(filepath):
    """
    读取完整 KTX 文件（含第一级 mipmap 纹理数据）

    Args:
        filepath: KTX 文件路径

    Returns:
        dict: 包含头部信息和纹理数据的字典，失败返回 None
    """
    try:
        with open(filepath, 'rb') as f:
            # ---- 验证标识符 ----
            identifier = f.read(12)
            if identifier != KTX_IDENTIFIER:
                raise ValueError("不是有效的 KTX 文件（标识符不匹配）")

            # ---- 字节序 ----
            endianness = struct.unpack('<I', f.read(4))[0]
            if endianness != 0x04030201:
                raise ValueError("不支持的字节序（仅支持小端）")

            # ---- 头部各字段 ----
            gl_type = struct.unpack('<I', f.read(4))[0]
            gl_type_size = struct.unpack('<I', f.read(4))[0]
            gl_format = struct.unpack('<I', f.read(4))[0]
            gl_internal_format = struct.unpack('<I', f.read(4))[0]
            gl_base_internal_format = struct.unpack('<I', f.read(4))[0]
            width = struct.unpack('<I', f.read(4))[0]
            height = struct.unpack('<I', f.read(4))[0]
            depth = struct.unpack('<I', f.read(4))[0]
            num_array = struct.unpack('<I', f.read(4))[0]
            num_faces = struct.unpack('<I', f.read(4))[0]
            num_mipmaps = struct.unpack('<I', f.read(4))[0]
            kv_size = struct.unpack('<I', f.read(4))[0]

            # ---- 跳过键值对数据 ----
            if kv_size > 0:
                f.seek(kv_size, 1)

            # ---- 读取第一级 mipmap 数据 ----
            # KTX 1.1: 每一级 mipmap 前有 4 字节 imageSize
            image_size_bytes = f.read(4)
            if len(image_size_bytes) < 4:
                raise ValueError("KTX 文件不完整（缺少 imageSize）")
            image_size = struct.unpack('<I', image_size_bytes)[0]

            data = f.read(image_size)
            if len(data) < image_size:
                raise ValueError(f"KTX 数据不完整（期望 {image_size}，实际 {len(data)}）")

            return {
                'gl_type': gl_type,
                'gl_type_size': gl_type_size,
                'gl_format': gl_format,
                'gl_internal_format': gl_internal_format,
                'gl_base_internal_format': gl_base_internal_format,
                'width': width,
                'height': height,
                'depth': depth,
                'num_array': num_array,
                'num_faces': num_faces,
                'num_mipmaps': num_mipmaps,
                'kv_size': kv_size,
                'image_size': image_size,
                'data': data,
            }

    except FileNotFoundError:
        print(f"❌ 文件不存在: {filepath}")
        return None
    except Exception as e:
        print(f"❌ 读取 KTX 文件失败: {e}")
        return None


def analyze_ktx_file(ktx_path):
    """
    分析 KTX 文件，返回完整的格式信息

    Args:
        ktx_path: KTX 文件路径

    Returns:
        dict: 分析结果，包含文件信息、匹配的格式对象等
    """
    info = read_ktx_header(ktx_path)
    if not info:
        return None

    gl_code = info['gl_internal_format']

    # 查找匹配的格式
    if gl_code in GL_CODE_TO_FORMAT:
        format_key, texture_format = GL_CODE_TO_FORMAT[gl_code]
    else:
        format_key, texture_format = None, None

    return {
        'file_path': ktx_path,
        'info': info,
        'format_key': format_key,
        'texture_format': texture_format,
        'gl_code': gl_code,
        'width': info['width'],
        'height': info['height'],
    }


def find_format_by_gl_code(gl_code):
    """
    根据 OpenGL 内部格式代码查找纹理格式

    Args:
        gl_code: OpenGL 内部格式代码

    Returns:
        (format_key, TextureFormat) 或 (None, None)
    """
    if gl_code in GL_CODE_TO_FORMAT:
        return GL_CODE_TO_FORMAT[gl_code]
    return None, None


def pad_to_multiple(dim, multiple=4):
    """
    将尺寸补齐到指定倍数的最近值

    Args:
        dim: 原始尺寸
        multiple: 倍数 (默认 4)

    Returns:
        int: 补齐后的尺寸
    """
    return ((dim + multiple - 1) // multiple) * multiple


def get_base_gl_format(fmt):
    """
    根据纹理格式获取基础 OpenGL 格式

    Args:
        fmt: TextureFormat 对象

    Returns:
        int: 基础 GL 格式代码
    """
    channels = fmt.channels
    if 'RGBA' in channels or '双通道' in channels:
        return 0x1908  # GL_RGBA
    elif '单通道' in channels or 'R)' in channels:
        return 0x1903  # GL_RED
    else:
        return 0x1907  # GL_RGB


def write_ktx(filepath, width, height, data, texture_format):
    """
    写入 KTX 文件

    Args:
        filepath: 输出文件路径
        width: 原始图像宽度
        height: 原始图像高度
        data: 纹理数据（压缩或未压缩的字节）
        texture_format: TextureFormat 对象
    """
    with open(filepath, 'wb') as f:
        # ---- KTX 文件头 ----
        # 标识符 (12 字节)
        f.write(KTX_IDENTIFIER)

        # 字节序 (4 字节) - 小端
        f.write(struct.pack('<I', 0x04030201))

        # glType (4 字节) - 0 表示压缩格式
        f.write(struct.pack('<I', 0))

        # glTypeSize (4 字节) - 1 表示压缩格式
        f.write(struct.pack('<I', 1))

        # glFormat (4 字节) - 0 表示压缩格式
        f.write(struct.pack('<I', 0))

        # glInternalFormat (4 字节) - 压缩格式的 OpenGL 内部格式
        f.write(struct.pack('<I', texture_format.gl_code))

        # glBaseInternalFormat (4 字节) - 基础格式
        f.write(struct.pack('<I', get_base_gl_format(texture_format)))

        # pixelWidth (4 字节)
        f.write(struct.pack('<I', width))

        # pixelHeight (4 字节)
        f.write(struct.pack('<I', height))

        # pixelDepth (4 字节) - 2D 纹理为 0
        f.write(struct.pack('<I', 0))

        # numberOfArrayElements (4 字节) - 非数组纹理为 0
        f.write(struct.pack('<I', 0))

        # numberOfFaces (4 字节) - 非立方体贴图为 1
        f.write(struct.pack('<I', 1))

        # numberOfMipmapLevels (4 字节) - 这里只有 1 级
        f.write(struct.pack('<I', 1))

        # bytesOfKeyValueData (4 字节) - 无键值对
        f.write(struct.pack('<I', 0))

        # ---- Mipmap 级别数据 ----
        data_size = len(data)

        if texture_format.category == 'uncompressed':
            # 未压缩格式需要 4 字节对齐
            padded_size = pad_to_multiple(data_size, 4)

            # imageSize (4 字节)
            f.write(struct.pack('<I', data_size))

            # 纹理数据
            f.write(data)

            # 对齐填充
            if padded_size > data_size:
                f.write(b'\x00' * (padded_size - data_size))
        else:
            # 压缩格式直接写入

            # imageSize (4 字节)
            f.write(struct.pack('<I', data_size))

            # 纹理数据
            f.write(data)


# ============================================================
#  图片处理与压缩函数
# ============================================================

def has_alpha(img):
    """
    自动检测图片是否包含透明通道

    检测逻辑:
    1. 如果图片模式是 RGBA/LA/PA，检查 alpha 通道是否有非 255 的值
    2. 如果是调色板模式，检查 transparency 信息

    Args:
        img: PIL Image 对象

    Returns:
        bool: 是否包含透明像素
    """
    # 检查 RGBA/LA/PA 模式
    if img.mode in ('RGBA', 'LA', 'PA'):
        # 转换为 RGBA 以确保有 alpha 通道
        img_rgba = img.convert('RGBA')
        # 提取 alpha 通道
        alpha_channel = np.array(img_rgba)[:, :, 3]
        # 只要有一个像素的 alpha < 255 就认为有透明
        return bool(np.any(alpha_channel < 255))

    # 检查调色板模式
    if img.mode == 'P' and 'transparency' in img.info:
        return True

    return False


def prepare_bgra_bytes(img, pad_multiple=4):
    """
    将 PIL 图片转换为 BGRA 字节格式，并自动补边到指定倍数

    ETC2/BC7 压缩库通常期望 BGRA 格式的输入数据。

    Args:
        img: PIL Image 对象
        pad_multiple: 补边的倍数 (默认 4)

    Returns:
        (bgra_bytes, padded_width, padded_height)
    """
    # 原始尺寸
    w, h = img.size

    # 计算补边后的尺寸
    new_w = pad_to_multiple(w, pad_multiple)
    new_h = pad_to_multiple(h, pad_multiple)

    # 确保是 RGBA 模式
    if img.mode != 'RGBA':
        img = img.convert('RGBA')

    # 如果需要补边
    if (new_w, new_h) != (w, h):
        # 创建新的透明画布
        padded_img = Image.new('RGBA', (new_w, new_h), (0, 0, 0, 0))
        # 将原图粘贴到左上角
        padded_img.paste(img, (0, 0))
        img = padded_img

    # 分离 RGBA 通道
    r, g, b, a = img.split()

    # 重新排列为 BGRA 顺序
    bgra = Image.merge('RGBA', (b, g, r, a))

    return bgra.tobytes(), new_w, new_h


def compress_etc2(data_bytes, width, height, fmt):
    """
    使用 etcpak 进行 ETC2 压缩

    Args:
        data_bytes: BGRA 格式的图片数据
        width: 图片宽度（已补边）
        height: 图片高度（已补边）
        fmt: TextureFormat 对象

    Returns:
        bytes: 压缩后的纹理数据
    """
    name = fmt.name

    # 根据具体格式选择对应的 etcpak 函数
    if 'R11' in name:
        # 单通道 R，有/无符号
        if 'SIGNED' in name:
            return etcpak.compress_etc2_r_signed(data_bytes, width, height)
        else:
            return etcpak.compress_etc2_r(data_bytes, width, height)

    elif 'RG11' in name:
        # 双通道 RG，有/无符号
        if 'SIGNED' in name:
            return etcpak.compress_etc2_rg_signed(data_bytes, width, height)
        else:
            return etcpak.compress_etc2_rg(data_bytes, width, height)

    elif 'RGBA8' in name or 'ALPHA8' in name:
        # RGBA 完整透明通道
        return etcpak.compress_etc2_rgba(data_bytes, width, height)

    elif 'RGB8A1' in name or 'SRGB8A1' in name:
        # RGB + 1 位透明
        return etcpak.compress_etc2_rgb_a1(data_bytes, width, height)

    elif 'ETC1' in name:
        # ETC1 格式
        return etcpak.compress_etc1_rgb(data_bytes, width, height)

    else:
        # 默认: RGB8 / SRGB8
        return etcpak.compress_etc2_rgb(data_bytes, width, height)


def compress_bc7(data_bytes, width, height, fmt):
    """
    使用 etcpak 进行 BC7 压缩

    Args:
        data_bytes: BGRA 格式的图片数据
        width: 图片宽度
        height: 图片高度
        fmt: TextureFormat 对象

    Returns:
        bytes: 压缩后的纹理数据
    """
    return etcpak.compress_bc7(data_bytes, width, height)


def compress_s3tc(img, fmt):
    """
    使用 Squish 库进行 S3TC (BC1-BC5/DXT1-5) 压缩

    Args:
        img: PIL Image 对象
        fmt: TextureFormat 对象

    Returns:
        bytes: 压缩后的纹理数据
    """
    if not SQUISH_AVAILABLE:
        raise RuntimeError("Squish 库不可用，无法压缩 S3TC 格式")

    # 根据格式选择颜色模式
    if 'RGBA' in fmt.channels:
        img = img.convert('RGBA')
    else:
        img = img.convert('RGB')

    # 补边到 4 的倍数
    w, h = img.size
    new_w = pad_to_multiple(w, 4)
    new_h = pad_to_multiple(h, 4)

    if (new_w, new_h) != (w, h):
        padded = Image.new(img.mode, (new_w, new_h))
        padded.paste(img, (0, 0))
        img = padded

    # 确保是 RGBA（Squish 需要）
    if img.mode != 'RGBA':
        img = img.convert('RGBA')

    # 转换为 numpy 数组再展平为字节
    pixels = np.array(img).flatten().tobytes()

    # 根据具体格式选择压缩标志
    name = fmt.name
    if 'BC1_RGB' in name:
        # BC1 RGB (DXT1 无透明)
        flags = squish.kDxt1
    elif 'BC1_RGBA' in name:
        # BC1 RGBA (DXT1 带 1 位透明)
        flags = squish.kDxt1 | squish.kColourIterativeClusterFit
    elif 'BC2' in name:
        # BC2 RGBA (DXT3)
        flags = squish.kDxt3
    elif 'BC3' in name:
        # BC3 RGBA (DXT5)
        flags = squish.kDxt5
    else:
        raise ValueError(f"不支持的 S3TC 格式: {name}")

    return squish.CompressImage(pixels, new_w, new_h, flags)


def compress_astc(img, fmt):
    """
    使用 astcenc 命令行工具进行 ASTC 压缩

    Args:
        img: PIL Image 对象
        fmt: TextureFormat 对象

    Returns:
        bytes: 压缩后的纹理数据
    """
    if not ASTC_AVAILABLE:
        raise RuntimeError("ASTC 编码器 (astcenc) 不可用")

    # 获取块大小
    block_w, block_h = fmt.block_size

    # 准备图片
    if 'RGBA' in fmt.channels:
        img = img.convert('RGBA')
    else:
        img = img.convert('RGB')

    # 保存为临时 PNG 文件
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        temp_input = f.name
        img.save(temp_input, 'PNG')

    try:
        # 创建临时输出文件
        with tempfile.NamedTemporaryFile(suffix='.astc', delete=False) as f:
            temp_output = f.name

        # 运行 astcenc 命令
        cmd = [
            'astcenc',
            '-cl',              # 压缩 LDR 图片
            temp_input,         # 输入文件
            temp_output,        # 输出文件
            f'{block_w}x{block_h}',  # 块大小
            '-thorough',        # 最高质量
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ASTC 压缩失败:\n{result.stderr}")

        # 读取压缩后的数据
        with open(temp_output, 'rb') as f:
            # ASTC 文件有 16 字节头部，跳过
            f.seek(16)
            compressed_data = f.read()

        return compressed_data

    finally:
        # 清理临时文件
        for temp_file in [temp_input, temp_output]:
            try:
                os.unlink(temp_file)
            except FileNotFoundError:
                pass


# ============================================================
#  纹理解码函数 (KTX → 图片)
# ============================================================

def _try_decode_with_names(data, width, height, names):
    """
    尝试用多个函数名调用 texture2ddecoder 的解码函数
    某些版本的 texture2ddecoder 函数名可能不同，通过这种方式提高兼容性
    """
    if not TEXTURE2DDECODER_AVAILABLE:
        raise RuntimeError("texture2ddecoder 未安装")

    errors = []
    for name in names:
        fn = getattr(texture2ddecoder, name, None)
        if fn is None:
            continue
        try:
            return fn(data, width, height)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue

    if not errors:
        raise RuntimeError(f"texture2ddecoder 中找不到以下任何函数: {names}")
    raise RuntimeError(f"所有候选函数均调用失败: {'; '.join(errors)}")


def bgra_to_pil(bgra_bytes, width, height):
    """
    将 BGRA 字节数据转换为 PIL Image (RGBA 模式)

    Args:
        bgra_bytes: BGRA 排列的像素数据
        width: 宽
        height: 高

    Returns:
        PIL.Image (RGBA)
    """
    expected = width * height * 4
    if len(bgra_bytes) != expected:
        raise ValueError(
            f"BGRA 数据长度不匹配: 期望 {expected}，实际 {len(bgra_bytes)}"
        )
    arr = np.frombuffer(bgra_bytes, dtype=np.uint8).reshape(height, width, 4)
    # BGRA → RGBA
    rgba = arr[:, :, [2, 1, 0, 3]]
    return Image.fromarray(rgba, 'RGBA')


def decode_uncompressed_texture(gl_internal_format, gl_format, data, width, height):
    """
    解码未压缩纹理数据

    Args:
        gl_internal_format: GL 内部格式
        gl_format: GL 格式
        data: 原始像素数据
        width: 宽
        height: 高

    Returns:
        PIL.Image (RGBA)
    """
    arr = np.frombuffer(data, dtype=np.uint8)

    if gl_internal_format == 0x1908 or gl_format == 0x1908:
        # RGBA8 / GL_RGBA
        expected = width * height * 4
        if len(arr) < expected:
            raise ValueError(f"RGBA 数据不足: 需要 {expected}，实际 {len(arr)}")
        arr = arr[:expected].reshape(height, width, 4)
        return Image.fromarray(arr, 'RGBA')

    elif gl_internal_format == 0x1907 or gl_format == 0x1907:
        # RGB8 / GL_RGB
        expected = width * height * 3
        if len(arr) < expected:
            raise ValueError(f"RGB 数据不足: 需要 {expected}，实际 {len(arr)}")
        arr = arr[:expected].reshape(height, width, 3)
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        rgba[:, :, :3] = arr
        rgba[:, :, 3] = 255
        return Image.fromarray(rgba, 'RGBA')

    elif gl_internal_format == 0x1903 or gl_format == 0x1903:
        # GL_RED
        expected = width * height
        if len(arr) < expected:
            raise ValueError(f"R 数据不足: 需要 {expected}，实际 {len(arr)}")
        arr = arr[:expected].reshape(height, width)
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        rgba[:, :, 0] = arr
        rgba[:, :, 1] = arr
        rgba[:, :, 2] = arr
        rgba[:, :, 3] = 255
        return Image.fromarray(rgba, 'RGBA')

    else:
        raise ValueError(
            f"不支持的未压缩格式: glInternalFormat=0x{gl_internal_format:04X}, "
            f"glFormat=0x{gl_format:04X}"
        )


def decode_compressed_via_texture2ddecoder(gl_code, data, width, height):
    """
    用 texture2ddecoder 解码压缩纹理（BC/ETC 系列）

    Args:
        gl_code: GL 内部格式代码
        data: 压缩数据
        width: 解码宽（已按块对齐）
        height: 解码高（已按块对齐）

    Returns:
        PIL.Image (RGBA)
    """
    if not TEXTURE2DDECODER_AVAILABLE:
        raise RuntimeError("texture2ddecoder 未安装，无法解码此格式")

    decoded = None

    # ---- BC1 (DXT1) ----
    if gl_code in (0x83F0, 0x83F1):
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_bc1', 'decode_dxt1'])

    # ---- BC2 (DXT3) ----
    elif gl_code == 0x83F2:
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_bc2', 'decode_dxt3'])

    # ---- BC3 (DXT5) ----
    elif gl_code == 0x83F3:
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_bc3', 'decode_dxt5'])

    # ---- BC7 ----
    elif gl_code in (0x8C43, 0x8C44, 0x8C45, 0x8C46,
                     0x8C47, 0x8C48, 0x8C49, 0x8C4A):
        decoded = _try_decode_with_names(data, width, height, ['decode_bc7'])

    # ---- ETC1 ----
    elif gl_code == 0x8D64:
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_etc1', 'decode_etc1_rgb'])

    # ---- ETC2 RGB8 / SRGB8 ----
    elif gl_code in (0x9274, 0x9275):
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_etc2_rgb', 'decode_etc2'])

    # ---- ETC2 RGB8A1 / SRGB8A1 ----
    elif gl_code in (0x9276, 0x9277):
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_etc2_rgba1', 'decode_etc2a1',
                                          'decode_etc2_rgb_a1'])

    # ---- ETC2 RGBA8 / SRGB8_ALPHA8 ----
    elif gl_code in (0x9278, 0x9279):
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_etc2_rgba', 'decode_etc2a8',
                                          'decode_etc2_rgba8'])

    # ---- EAC R11 ----
    elif gl_code in (0x9270, 0x9271):
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_eac_r11', 'decode_eac_r'])
        # 单通道解压结果可能是 RG 或 R 数据
        if decoded is not None and len(decoded) == width * height * 2:
            arr = np.frombuffer(decoded, dtype=np.uint8).reshape(height, width, 2)
            rgba = np.zeros((height, width, 4), dtype=np.uint8)
            rgba[:, :, 0] = arr[:, :, 0]
            rgba[:, :, 1] = arr[:, :, 0]
            rgba[:, :, 2] = arr[:, :, 0]
            rgba[:, :, 3] = 255
            return Image.fromarray(rgba, 'RGBA')

    # ---- EAC RG11 ----
    elif gl_code in (0x9272, 0x9273):
        decoded = _try_decode_with_names(data, width, height,
                                         ['decode_eac_rg11', 'decode_eac_rg'])
        if decoded is not None and len(decoded) == width * height * 2:
            arr = np.frombuffer(decoded, dtype=np.uint8).reshape(height, width, 2)
            rgba = np.zeros((height, width, 4), dtype=np.uint8)
            rgba[:, :, 0] = arr[:, :, 0]
            rgba[:, :, 1] = arr[:, :, 1]
            rgba[:, :, 2] = 0
            rgba[:, :, 3] = 255
            return Image.fromarray(rgba, 'RGBA')

    if decoded is None:
        raise ValueError(f"texture2ddecoder 无法解码此格式: 0x{gl_code:04X}")

    # 默认返回值视为 BGRA
    if len(decoded) == width * height * 4:
        return bgra_to_pil(decoded, width, height)
    elif len(decoded) == width * height * 3:
        arr = np.frombuffer(decoded, dtype=np.uint8).reshape(height, width, 3)
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        rgba[:, :, :3] = arr
        rgba[:, :, 3] = 255
        return Image.fromarray(rgba, 'RGBA')
    else:
        raise ValueError(
            f"texture2ddecoder 返回了异常长度的数据: {len(decoded)} "
            f"(期望 {width * height * 4})"
        )


def decode_astc_via_astcenc(data, width, height, block_size):
    """
    用 astcenc 命令行解压 ASTC 数据

    Args:
        data: ASTC 数据（不含文件头）
        width: 解码宽（已按块对齐）
        height: 解码高（已按块对齐）
        block_size: (block_w, block_h)

    Returns:
        PIL.Image (RGBA)
    """
    if not ASTC_AVAILABLE:
        raise RuntimeError("ASTC 编码器 (astcenc) 不可用，无法解码 ASTC")

    block_w, block_h = block_size

    # ---- 构造 ASTC 文件头 ----
    # 格式：
    #   magic(4) 0x5CA1AB13
    #   block_x(1) block_y(1) block_z(1)
    #   dim_x(3 little-endian)
    #   dim_y(3 little-endian)
    #   dim_z(3 little-endian)
    astc_header = bytearray()
    astc_header += struct.pack('<I', 0x5CA1AB13)
    astc_header += bytes([block_w & 0xFF, block_h & 0xFF, 1])
    astc_header += width.to_bytes(3, 'little')
    astc_header += height.to_bytes(3, 'little')
    astc_header += (1).to_bytes(3, 'little')

    # ---- 写临时 ASTC 文件 ----
    with tempfile.NamedTemporaryFile(suffix='.astc', delete=False) as f:
        temp_input = f.name
        f.write(astc_header)
        f.write(data)

    # ---- 临时 PNG 输出 ----
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        temp_output = f.name

    try:
        # ---- 调用 astcenc 解压 ----
        cmd = ['astcenc', '-dl', temp_input, temp_output]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ASTC 解压失败:\n{result.stderr}")

        # ---- 读取解压后的 PNG ----
        img = Image.open(temp_output)
        return img.convert('RGBA')

    finally:
        for temp_file in [temp_input, temp_output]:
            try:
                os.unlink(temp_file)
            except FileNotFoundError:
                pass


def get_padded_size(orig_w, orig_h, fmt):
    """
    根据格式推断补边后的尺寸

    - ASTC：按块大小对齐
    - ETC2/BC7/S3TC：4 的倍数
    - 未压缩：原始尺寸
    """
    if fmt.category == 'astc' and fmt.block_size:
        bw, bh = fmt.block_size
        return pad_to_multiple(orig_w, bw), pad_to_multiple(orig_h, bh)
    elif fmt.category in ('etc2', 'bc7', 's3tc'):
        return pad_to_multiple(orig_w, 4), pad_to_multiple(orig_h, 4)
    else:
        return orig_w, orig_h


def decode_ktx_to_image(ktx_data):
    """
    将 KTX 数据完整解码为 PIL Image

    Args:
        ktx_data: read_ktx_full() 返回的字典

    Returns:
        PIL.Image (RGBA)
    """
    gl_internal_format = ktx_data['gl_internal_format']
    gl_type = ktx_data['gl_type']
    gl_format = ktx_data['gl_format']
    width = ktx_data['width']
    height = ktx_data['height']
    data = ktx_data['data']

    # ---- 查找匹配格式 ----
    fmt_key, fmt = find_format_by_gl_code(gl_internal_format)
    if fmt is None:
        raise ValueError(
            f"不支持的 KTX 格式: glInternalFormat=0x{gl_internal_format:04X}\n"
            f"   glType=0x{gl_type:04X}, glFormat=0x{gl_format:04X}"
        )

    # ---- 检查解码能力 ----
    if not fmt.is_decodable:
        missing = []
        if fmt.category == 'astc' and not ASTC_AVAILABLE:
            missing.append("ASTC 编码器 (astcenc)")
        if fmt.category in ('etc2', 'bc7', 's3tc') and not TEXTURE2DDECODER_AVAILABLE:
            missing.append("texture2ddecoder (pip install texture2ddecoder)")
        raise RuntimeError(
            f"缺少解码依赖: {', '.join(missing)}\n"
            f"   → 选择 [6] 自动安装，或 [7] 查看手动命令"
        )

    # ---- 计算解码尺寸（补边后） ----
    padded_w, padded_h = get_padded_size(width, height, fmt)

    # ---- 根据格式类别解码 ----
    if fmt.category == 'uncompressed':
        img = decode_uncompressed_texture(
            gl_internal_format, gl_format, data, width, height
        )
        # 未压缩无需裁剪
        return img

    elif fmt.category == 'astc':
        img = decode_astc_via_astcenc(data, padded_w, padded_h, fmt.block_size)
        # 裁剪到原始尺寸
        img = img.crop((0, 0, width, height))
        return img

    else:
        # ETC2 / BC7 / S3TC
        img = decode_compressed_via_texture2ddecoder(
            gl_internal_format, data, padded_w, padded_h
        )
        # 裁剪到原始尺寸
        img = img.crop((0, 0, width, height))
        return img


def save_image(img, output_path, quality=95):
    """
    保存 PIL Image 到文件（按扩展名选择格式）

    Args:
        img: PIL Image 对象
        output_path: 输出文件路径
        quality: JPEG/WebP 质量 (0-100)
    """
    ext = Path(output_path).suffix.lower()

    # 若扩展名缺失或未知，默认 PNG
    if not ext or ext not in SUPPORTED_OUTPUT_IMAGE_FORMATS:
        if not ext:
            output_path = str(output_path) + '.png'
            ext = '.png'
        else:
            print(f"⚠️  未知的图片扩展名 {ext}，将使用 PNG 格式")
            output_path = str(Path(output_path).with_suffix('.png'))
            ext = '.png'

    # JPEG 不支持透明通道
    if ext in ('.jpg', '.jpeg'):
        if img.mode == 'RGBA':
            # 合成到白底
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background
        else:
            img = img.convert('RGB')
        img.save(output_path, 'JPEG', quality=quality)

    elif ext == '.png':
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        img.save(output_path, 'PNG')

    elif ext == '.bmp':
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        img.save(output_path, 'BMP')

    elif ext == '.webp':
        if img.mode != 'RGBA' and img.mode != 'RGB':
            img = img.convert('RGBA')
        img.save(output_path, 'WebP', quality=quality)

    elif ext in ('.tiff', '.tif'):
        img.save(output_path, 'TIFF')

    elif ext == '.tga':
        img.save(output_path, 'TGA')

    else:
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        img.save(output_path, 'PNG')


def convert_ktx_to_image(input_path, output_path, verbose=True, quality=95):
    """
    核心函数: 将 KTX 文件解压为通用图片

    Args:
        input_path: 输入 KTX 文件路径
        output_path: 输出图片路径
        verbose: 是否输出详细信息
        quality: JPEG/WebP 质量

    Returns:
        bool: 转换是否成功
    """
    try:
        if verbose:
            print(f"📦 输入文件: {input_path}")

        # ---- 1. 读取完整 KTX 数据 ----
        ktx_data = read_ktx_full(input_path)
        if ktx_data is None:
            return False

        if verbose:
            print(f"   文件大小: {os.path.getsize(input_path):,} 字节")
            print(f"   glInternalFormat: 0x{ktx_data['gl_internal_format']:04X}")
            print(f"   glType:           0x{ktx_data['gl_type']:04X}")
            print(f"   glFormat:         0x{ktx_data['gl_format']:04X}")
            print(f"   尺寸: {ktx_data['width']} × {ktx_data['height']}")
            print(f"   mipmap 层数: {ktx_data['num_mipmaps']}")
            print(f"   纹理数据: {len(ktx_data['data']):,} 字节")

        # ---- 2. 查找格式 ----
        fmt_key, fmt = find_format_by_gl_code(ktx_data['gl_internal_format'])
        if fmt is not None:
            if verbose:
                print(f"   格式名称: {fmt.name}")
                print(f"   类别: {fmt.category}")
                print(f"   通道: {fmt.channels}")
        else:
            if verbose:
                print(f"   ⚠️  格式代码不在已知列表中")

        # ---- 3. 解码 ----
        if verbose:
            print(f"🔧 正在解码，请稍候...")

        img = decode_ktx_to_image(ktx_data)

        # ---- 4. 保存 ----
        save_image(img, output_path, quality=quality)

        # ---- 5. 输出信息 ----
        if verbose:
            file_size = os.path.getsize(output_path)
            print()
            print(f"   ✅ 转换成功！")
            print(f"   📁 输出文件: {output_path}")
            print(f"   📐 输出尺寸: {img.size[0]} × {img.size[1]} 像素")
            print(f"   💾 文件大小: {file_size:,} 字节 ({file_size/1024:.1f} KB)")
            if ktx_data['width'] != img.size[0] or ktx_data['height'] != img.size[1]:
                print(
                    f"   ℹ️  KTX 头部记录尺寸: {ktx_data['width']}×{ktx_data['height']},"
                    f" 实际像素: {img.size[0]}×{img.size[1]}"
                )

        return True

    except FileNotFoundError:
        print(f"❌ 文件不存在: {input_path}")
        return False
    except Exception as e:
        print(f"❌ 转换失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
#  核心转换函数 (图片 → KTX)
# ============================================================

def convert_image_to_ktx(input_path, output_path, texture_format, verbose=True):
    """
    核心转换函数: 将图片转换为指定格式的 KTX 文件

    功能:
    - 自动检测输入图片格式 (PNG/JPG/BMP/WebP等)
    - 自动检测透明通道
    - 自动补边到 4 的倍数
    - 根据目标格式选择正确的压缩器

    Args:
        input_path: 输入图片路径
        output_path: 输出 KTX 文件路径
        texture_format: TextureFormat 对象，目标纹理格式
        verbose: 是否输出详细信息

    Returns:
        bool: 转换是否成功
    """
    try:
        # ---- 1. 打开图片 ----
        img = Image.open(input_path)
        fmt = texture_format

        # 检查格式是否可用
        if not fmt.is_available:
            missing_parts = []
            if fmt.requires_s3tc:
                missing_parts.append("Squish 库 (pip install squish)")
            if fmt.requires_astc:
                missing_parts.append("ASTC 编码器 (pkg install astc-encoder)")
            if fmt.requires_etcpak:
                missing_parts.append("etcpak 库 (pip install etcpak)")

            print(f"❌ 格式 '{fmt.name}' 当前不可用")
            print(f"   缺少依赖: {', '.join(missing_parts)}")
            print(f"   → 运行选项 [6] 自动安装，或 [7] 查看手动命令")
            return False

        # ---- 2. 显示输入信息 ----
        if verbose:
            # 检测文件格式
            ext = Path(input_path).suffix.lower()
            detected_format = SUPPORTED_INPUT_FORMATS.get(ext, img.format or '未知')

            # 检测透明通道
            alpha_detected = has_alpha(img)

            print(f"📷 输入文件: {input_path}")
            print(f"   检测格式: {detected_format}")
            print(f"   像素模式: {img.mode}")
            print(f"   原始尺寸: {img.size[0]} × {img.size[1]} 像素")
            print(f"   透明通道: {'✅ 有' if alpha_detected else '❌ 无'}")
            print(f"🎯 目标格式: {fmt.name}")
            print(f"   GL代码: 0x{fmt.gl_code:04X}")
            print(f"   类别: {fmt.category}")
            print(f"   通道: {fmt.channels}")
            print(f"🔧 正在压缩，请稍候...")

        # ---- 3. 保存原始尺寸 ----
        w_orig, h_orig = img.size

        # ---- 4. 根据格式类别选择压缩方法 ----
        if fmt.category == 'etc2':
            # ETC2 / EAC 压缩
            data_bytes, padded_w, padded_h = prepare_bgra_bytes(img)
            compressed_data = compress_etc2(data_bytes, padded_w, padded_h, fmt)

        elif fmt.category == 'bc7':
            # BC7 压缩
            data_bytes, padded_w, padded_h = prepare_bgra_bytes(img)
            compressed_data = compress_bc7(data_bytes, padded_w, padded_h, fmt)

        elif fmt.category == 's3tc':
            # S3TC (BC1-BC5) 压缩
            compressed_data = compress_s3tc(img, fmt)

        elif fmt.category == 'astc':
            # ASTC 压缩
            compressed_data = compress_astc(img, fmt)

        elif fmt.category == 'uncompressed':
            # 未压缩格式
            if 'RGBA' in fmt.channels:
                img = img.convert('RGBA')
            else:
                img = img.convert('RGB')
            compressed_data = np.array(img).flatten().tobytes()

        else:
            raise ValueError(f"未知的格式类别: {fmt.category}")

        # ---- 5. 写入 KTX 文件 ----
        write_ktx(output_path, w_orig, h_orig, compressed_data, fmt)

        # ---- 6. 显示输出信息 ----
        if verbose:
            file_size = os.path.getsize(output_path)
            print()
            print(f"   ✅ 转换成功！")
            print(f"   📁 输出文件: {output_path}")
            print(f"   📐 格式: {fmt.name}")
            print(f"   📏 尺寸: {w_orig} × {h_orig} 像素")
            print(f"   💾 文件大小: {file_size:,} 字节 ({file_size/1024:.1f} KB)")

            # 如果补边了，给出提示
            if fmt.category in ('etc2', 'bc7'):
                padded_w = pad_to_multiple(w_orig)
                padded_h = pad_to_multiple(h_orig)
                if (padded_w != w_orig) or (padded_h != h_orig):
                    print(f"   ℹ️  已自动补边至 {padded_w} × {padded_h} (ETC2/BC7要求4的倍数)")

        return True

    except FileNotFoundError:
        print(f"❌ 文件不存在: {input_path}")
        return False
    except Exception as e:
        print(f"❌ 转换失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
#  [模式 1] 简易模式 - 原 Sky-png→ktx.py 功能
# ============================================================

def simple_mode():
    """
    简易模式: 快速 PNG → KTX 转换

    功能:
    - 输入一张图片，自动检测透明通道
    - 自动选择 ETC2_RGB8 (无透明) 或 ETC2_RGBA8_EAC (有透明)
    - 支持 --rgb / --rgba 强制指定颜色模式
    - 自动补边到 4 的倍数
    - 输出 KTX 文件

    对应原始脚本 Sky-png→ktx.py 的完整功能
    """
    print()
    print("=" * 60)
    print("🚀 简易模式 - 图片 → KTX 快速转换")
    print("=" * 60)
    print()
    print("功能说明:")
    print("  • 自动检测透明通道，选择 RGB 或 RGBA 模式")
    print("  • 自动补边到 4 的倍数")
    print("  • 输出 ETC2 压缩的 KTX 文件")
    print()

    # ---- 步骤 1: 输入图片 ----
    while True:
        input_path = input("📥 输入图片路径: ").strip().strip("'\"")
        if not input_path:
            print("❌ 路径不能为空，请重新输入")
            continue
        if not os.path.exists(input_path):
            print(f"❌ 文件不存在: {input_path}")
            continue

        # 检查文件扩展名
        ext = Path(input_path).suffix.lower()
        if ext and ext not in SUPPORTED_INPUT_FORMATS:
            print(f"⚠️  不常见的文件格式: {ext}")
            print(f"   支持的格式: {', '.join(sorted(set(SUPPORTED_INPUT_FORMATS.values())))}")
            confirm = input("   是否仍要尝试？(y/n，默认 n): ").strip().lower()
            if confirm != 'y':
                continue
        break

    # ---- 步骤 2: 选择颜色模式 ----
    print()
    print("🎨 颜色模式选择:")
    print("   [1] 自动检测（推荐）- 有透明用 RGBA，无透明用 RGB")
    print("   [2] 强制 RGB - 忽略透明通道")
    print("   [3] 强制 RGBA - 保留透明通道")

    while True:
        mode_choice = input("   请选择 (1/2/3，默认 1): ").strip()
        if not mode_choice:
            mode_choice = '1'
        if mode_choice in ('1', '2', '3'):
            break
        print("❌ 无效选择，请输入 1、2 或 3")

    force_rgb = (mode_choice == '2')
    force_rgba = (mode_choice == '3')

    # ---- 步骤 3: 输出路径 ----
    default_output = str(Path(input_path).with_suffix('.ktx'))
    output_path = input(f"\n📤 输出路径 (直接回车使用默认): ").strip().strip("'\"")
    if not output_path:
        output_path = default_output
    print(f"   输出: {output_path}")

    print()
    print("=" * 60)

    # ---- 执行转换 ----
    try:
        # 打开图片
        img = Image.open(input_path)
        original_mode = img.mode
        original_size = img.size

        print(f"\n📷 原始图片信息:")
        print(f"   路径: {input_path}")
        print(f"   格式: {img.format}")
        print(f"   模式: {original_mode}")
        print(f"   尺寸: {original_size[0]} × {original_size[1]} 像素")

        # 判断是否保留透明通道
        if force_rgba:
            has_alpha_result = True
            mode_description = "强制 RGBA 模式"
        elif force_rgb:
            has_alpha_result = False
            mode_description = "强制 RGB 模式"
        else:
            has_alpha_result = has_alpha(img)
            mode_description = "自动检测 - " + ("RGBA (检测到透明像素)" if has_alpha_result else "RGB (无透明像素)")

        print(f"   颜色模式: {mode_description}")

        # 转换为最终格式
        if has_alpha_result:
            img = img.convert('RGBA')
        else:
            img = img.convert('RGB')

        w, h = img.size
        new_w = pad_to_multiple(w, 4)
        new_h = pad_to_multiple(h, 4)

        # 补边
        if (new_w, new_h) != (w, h):
            print(f"   ⚠️  尺寸 {w}×{h} 不是 4 的倍数，正在补边至 {new_w}×{new_h}...")
            padded_img = Image.new(img.mode, (new_w, new_h), (0, 0, 0, 0))
            padded_img.paste(img, (0, 0))
            img = padded_img
            # 边缘镜像填充（简单方案：重复最后一排像素）
            # 注意：这里使用简单黑边填充，生产环境可改为镜像

        print(f"   🔧 正在压缩...")

        if has_alpha_result:
            # RGBA 格式: 转换为 BGRA 字节并压缩
            data_bytes = img.tobytes()
            compressed = etcpak.compress_etc2_rgba(data_bytes, new_w, new_h)
            output_format = FORMATS['etc2_rgba8']
        else:
            # RGB 格式: 转换为 BGRA 字节并压缩
            rgba_img = img.convert('RGBA')
            r, g, b, a = rgba_img.split()
            bgra = Image.merge('RGBA', (b, g, r, a))
            data_bytes = bgra.tobytes()
            compressed = etcpak.compress_etc2_rgb(data_bytes, new_w, new_h)
            output_format = FORMATS['etc2_rgb8']

        # 写入 KTX 文件
        write_ktx(output_path, w, h, compressed, output_format)

        # 显示结果
        file_size = os.path.getsize(output_path)
        print()
        print(f"   ✅ 转换成功！")
        print(f"   📁 输出文件: {output_path}")
        print(f"   📐 输出格式: {output_format.name}")
        print(f"   颜色格式: {'RGBA (ETC2 + EAC)' if has_alpha_result else 'RGB (ETC2)'}")
        print(f"   📏 原始尺寸: {w} × {h} 像素")
        print(f"   💾 文件大小: {file_size:,} 字节 ({file_size/1024:.1f} KB)")
        print()
        return True

    except Exception as e:
        print(f"\n❌ 转换失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
#  [模式 2] 参考模式 - 从现有 KTX 匹配格式
# ============================================================

def reference_mode():
    """
    参考模式: 从现有 KTX 文件读取格式，用相同格式转换新图片

    工作流程:
    1. 输入一个现有的 KTX 文件（作为参考）
    2. 脚本自动解析该文件的格式信息
    3. 输入一张新图片
    4. 用相同的纹理格式转换新图片
    """
    print()
    print("=" * 60)
    print("📋 参考模式 - 从现有 KTX 匹配格式")
    print("=" * 60)
    print()
    print("功能说明:")
    print("  读取现有 KTX 文件的格式信息")
    print("  用相同的格式转换新的图片")
    print("  无需手动选择和记忆格式参数")
    print()

    # ---- 步骤 1: 输入参考 KTX 文件 ----
    while True:
        ktx_path = input("📋 参考 KTX 文件路径: ").strip().strip("'\"")
        if not ktx_path:
            print("❌ 路径不能为空")
            continue
        if not os.path.exists(ktx_path):
            print(f"❌ 文件不存在: {ktx_path}")
            continue
        if not ktx_path.lower().endswith('.ktx'):
            print("⚠️  文件扩展名不是 .ktx")
            confirm = input("   是否仍要尝试？(y/n): ").strip().lower()
            if confirm != 'y':
                continue
        break

    # ---- 步骤 2: 解析 KTX 文件 ----
    print(f"\n🔍 正在分析 KTX 文件: {ktx_path}")
    ktx_info = analyze_ktx_file(ktx_path)

    if not ktx_info:
        print("❌ 无法解析 KTX 文件，可能不是有效的 KTX 格式")
        return False

    # 显示分析结果
    print()
    print("📋 KTX 文件分析结果:")
    print(f"   文件路径: {ktx_info['file_path']}")
    print(f"   图像尺寸: {ktx_info['width']} × {ktx_info['height']} 像素")
    print(f"   GL 内部格式代码: 0x{ktx_info['gl_code']:04X}")

    if ktx_info['texture_format']:
        fmt = ktx_info['texture_format']
        print(f"   格式名称: {fmt.name}")
        print(f"   格式类别: {fmt.category}")
        print(f"   通道类型: {fmt.channels}")

        # 检查可用性
        if not fmt.is_available:
            missing_parts = []
            if fmt.requires_s3tc:
                missing_parts.append("Squish 库")
            if fmt.requires_astc:
                missing_parts.append("ASTC 编码器")
            if fmt.requires_etcpak:
                missing_parts.append("etcpak 库")

            print(f"   ⚠️  当前环境缺少此格式的依赖: {', '.join(missing_parts)}")
            print(f"   → 选择选项 [6] 安装依赖后再试")
            return False
    else:
        print(f"   ⚠️  此格式代码 (0x{ktx_info['gl_code']:04X}) 不在已知格式列表中")
        print(f"   脚本无法处理此格式")
        return False

    # ---- 步骤 3: 输入要转换的图片 ----
    while True:
        img_path = input("\n🖼️  要转换的图片路径: ").strip().strip("'\"")
        if not img_path:
            print("❌ 路径不能为空")
            continue
        if not os.path.exists(img_path):
            print(f"❌ 文件不存在: {img_path}")
            continue
        break

    # ---- 步骤 4: 输出路径 ----
    default_output = str(Path(img_path).with_suffix('.ktx'))
    output_path = input(f"📤 输出路径 (直接回车默认): ").strip().strip("'\"")
    if not output_path:
        output_path = default_output
    print(f"   输出: {output_path}")

    print()
    print("=" * 60)
    print(f"🎯 使用格式: {ktx_info['texture_format'].name} (来自参考文件)")
    print(f"   GL 代码: 0x{ktx_info['texture_format'].gl_code:04X}")
    print()

    # ---- 执行转换 ----
    return convert_image_to_ktx(img_path, output_path, ktx_info['texture_format'])


# ============================================================
#  [模式 3] 专家模式 - 全格式选择
# ============================================================

def select_format_interactive():
    """
    交互式选择输出纹理格式

    按类别显示所有可用格式，用户输入编号选择

    Returns:
        (format_key, TextureFormat) 或 (None, None) 如果用户取消/无可用格式
    """
    print()
    print("=" * 60)
    print("🎨 选择输出纹理格式")
    print("=" * 60)

    all_formats = {}   # {编号: 格式键}
    index = 1
    default_choice = None

    # 按类别显示
    for category_name, category_info in FORMAT_CATEGORIES.items():
        # 检查该类别是否有可用格式
        available_in_category = [
            k for k in category_info['keys']
            if FORMATS[k].is_available
        ]

        if not available_in_category:
            # 该类别全部不可用
            all_unavailable = all(
                not FORMATS[k].is_available
                for k in category_info['keys']
            )
            if all_unavailable:
                status = "（全部不可用，已隐藏）"
            else:
                status = ""
            continue

        # 显示类别标题
        print(f"\n{'─' * 55}")
        print(f"📦 {category_name}")
        print(f"   {category_info['desc']}")
        print(f"{'─' * 55}")

        # 显示类别中的格式
        for fmt_key in category_info['keys']:
            fmt = FORMATS[fmt_key]

            if not fmt.is_available:
                continue  # 跳过不可用的格式

            default_mark = " ⭐默认" if fmt.default else ""
            print(f"  [{index:2d}] {fmt.name:<35s}{default_mark}")
            print(f"       通道: {fmt.channels}")

            all_formats[index] = fmt_key

            # 记录第一个默认格式
            if fmt.default and default_choice is None:
                default_choice = index

            index += 1

    if not all_formats:
        print("\n❌ 当前没有可用的纹理格式！")
        print("   请先安装所需依赖:")
        print("   → 选择选项 [6] 自动安装")
        print("   → 或选项 [7] 查看手动命令")
        return None, None

    # 获取用户选择
    print(f"\n{'─' * 55}")

    while True:
        if default_choice:
            prompt = f"请选择格式编号 (1-{len(all_formats)}) [默认: {default_choice}]: "
        else:
            prompt = f"请选择格式编号 (1-{len(all_formats)}): "

        choice = input(prompt).strip()

        if not choice and default_choice:
            choice = str(default_choice)

        try:
            idx = int(choice)
            if idx in all_formats:
                selected_key = all_formats[idx]
                selected_fmt = FORMATS[selected_key]
                print(f"\n✅ 已选择: {selected_fmt.name}")
                print(f"   类别: {selected_fmt.category}")
                print(f"   通道: {selected_fmt.channels}")
                return selected_key, selected_fmt
            else:
                print(f"❌ 无效选择，请输入 1-{len(all_formats)}")
        except ValueError:
            print("❌ 请输入有效的数字")


def expert_mode():
    """
    专家模式: 全格式选择转换

    功能:
    - 支持所有纹理格式 (ETC2/EAC, BC7/BPTC, S3TC/BC1-BC5, ASTC, 未压缩)
    - 交互式选择目标格式
    - 自动检测透明通道和补边
    """
    print()
    print("=" * 60)
    print("🎨 专家模式 - 全格式纹理转换")
    print("=" * 60)
    print()

    # ---- 步骤 1: 输入图片 ----
    while True:
        input_path = input("📥 输入图片路径: ").strip().strip("'\"")
        if not input_path:
            print("❌ 路径不能为空")
            continue
        if not os.path.exists(input_path):
            print(f"❌ 文件不存在: {input_path}")
            continue

        # 格式检查
        ext = Path(input_path).suffix.lower()
        if ext and ext not in SUPPORTED_INPUT_FORMATS:
            print(f"⚠️  不常见的文件格式: {ext}")
            print(f"   支持的格式: {', '.join(sorted(set(SUPPORTED_INPUT_FORMATS.values())))}")
            confirm = input("   是否仍要尝试？(y/n): ").strip().lower()
            if confirm != 'y':
                continue
        break

    # ---- 步骤 2: 选择格式 ----
    result = select_format_interactive()
    if not result or result[0] is None:
        return
    format_key, texture_format = result

    # ---- 步骤 3: 输出路径 ----
    default_output = str(Path(input_path).with_suffix('.ktx'))
    output_path = input(f"\n📤 输出路径 (直接回车默认): ").strip().strip("'\"")
    if not output_path:
        output_path = default_output
    print(f"   输出: {output_path}")

    print()
    print("=" * 60)

    # ---- 执行转换 ----
    convert_image_to_ktx(input_path, output_path, texture_format)


# ============================================================
#  [模式 4] KTX → 图片 模式
# ============================================================

def ktx_to_image_mode():
    """
    KTX → 图片 模式
    将任意格式的 KTX 文件解压为通用图片格式
    """
    print()
    print("=" * 60)
    print("🖼️  KTX → 图片 转换")
    print("=" * 60)
    print()
    print("功能说明:")
    print("  • 解析 KTX 文件中的格式信息")
    print("  • 支持未压缩 / ETC2 / BC1-BC5 / BC7 / ASTC 解压")
    print("  • 自动裁剪补边，还原原始尺寸")
    print("  • 输出 PNG / JPG / BMP / WebP / TIFF / TGA")
    print()

    # ---- 依赖检查 ----
    if not DEPENDENCY_STATUS['pillow']['available'] or not DEPENDENCY_STATUS['numpy']['available']:
        print("❌ 基础依赖 (Pillow, NumPy) 未安装，无法执行")
        print("   请先选择 [6] 安装依赖")
        return False

    # ---- 步骤 1: 输入 KTX ----
    while True:
        ktx_path = input("📦 输入 KTX 文件路径: ").strip().strip("'\"")
        if not ktx_path:
            print("❌ 路径不能为空")
            continue
        if not os.path.exists(ktx_path):
            print(f"❌ 文件不存在: {ktx_path}")
            continue
        if not ktx_path.lower().endswith('.ktx'):
            print("⚠️  文件扩展名不是 .ktx")
            confirm = input("   是否仍要尝试？(y/n): ").strip().lower()
            if confirm != 'y':
                continue
        break

    # ---- 步骤 2: 预分析，检查格式可解码性 ----
    print(f"\n🔍 正在分析 KTX 文件: {ktx_path}")
    ktx_info = read_ktx_full(ktx_path)

    if not ktx_info:
        print("❌ 无法读取 KTX 文件")
        return False

    gl_code = ktx_info['gl_internal_format']
    fmt_key, fmt = find_format_by_gl_code(gl_code)

    print(f"   glInternalFormat: 0x{gl_code:04X}")
    print(f"   尺寸: {ktx_info['width']} × {ktx_info['height']}")
    if fmt:
        print(f"   格式名称: {fmt.name}")
        print(f"   格式类别: {fmt.category}")
        print(f"   通道: {fmt.channels}")

        # 检查解码依赖
        if not fmt.is_decodable:
            missing = []
            if fmt.category == 'astc' and not ASTC_AVAILABLE:
                missing.append("ASTC 编码器 (astcenc)")
            if fmt.category in ('etc2', 'bc7', 's3tc') and not TEXTURE2DDECODER_AVAILABLE:
                missing.append("texture2ddecoder")
            print(f"\n⚠️  缺少解压所需依赖: {', '.join(missing)}")
            print(f"   → 选择 [6] 自动安装，或 [7] 查看手动命令")
            return False
    else:
        print(f"   ⚠️  格式代码不在已知列表中，将尝试解码")

    # ---- 步骤 3: 选择输出格式 ----
    print()
    print("🖼️  选择输出图片格式:")
    print("   [1] PNG  (推荐，支持透明)")
    print("   [2] JPG  (有损压缩，不支持透明)")
    print("   [3] BMP  (无压缩，体积较大)")
    print("   [4] WebP (现代格式，体积小)")
    print("   [5] TIFF (无损，专业用途)")
    print("   [6] TGA  (简单无损)")

    ext_map = {
        '1': '.png',
        '2': '.jpg',
        '3': '.bmp',
        '4': '.webp',
        '5': '.tiff',
        '6': '.tga',
    }
    while True:
        choice = input("   请选择 (1-6，默认 1): ").strip()
        if not choice:
            choice = '1'
        if choice in ext_map:
            break
        print("❌ 无效选择，请输入 1-6")
    out_ext = ext_map[choice]

    # ---- 步骤 4: 输出路径 ----
    default_output = str(Path(ktx_path).with_suffix(out_ext))
    output_path = input(f"\n📤 输出路径 (直接回车默认): ").strip().strip("'\"")
    if not output_path:
        output_path = default_output
    print(f"   输出: {output_path}")

    # ---- 步骤 5: JPEG/WebP 质量 ----
    quality = 95
    if out_ext in ('.jpg', '.jpeg', '.webp'):
        q_str = input("   JPEG/WebP 质量 (0-100，默认 95): ").strip()
        if q_str.isdigit():
            q = int(q_str)
            if 0 <= q <= 100:
                quality = q

    print()
    print("=" * 60)

    # ---- 执行转换 ----
    return convert_ktx_to_image(ktx_path, output_path, verbose=True, quality=quality)


# ============================================================
#  命令行动态简易模式处理
# ============================================================

def simple_mode_command(input_path, output_path, force_rgb=False, force_rgba=False, quiet=False):
    """
    命令行简易模式处理函数
    对应 --simple 参数
    """
    if not DEPENDENCY_STATUS['etcpak']['available']:
        print("❌ etcpak 库未安装，简易模式不可用")
        print("   运行: pip install etcpak")
        return False

    if not DEPENDENCY_STATUS['pillow']['available'] or not DEPENDENCY_STATUS['numpy']['available']:
        print("❌ 基础依赖未安装")
        print("   运行: pip install Pillow numpy")
        return False

    try:
        img = Image.open(input_path)

        if force_rgba:
            has_alpha_result = True
        elif force_rgb:
            has_alpha_result = False
        else:
            has_alpha_result = has_alpha(img)

        # 转换
        img = img.convert('RGBA') if has_alpha_result else img.convert('RGB')
        w, h = img.size
        new_w, new_h = pad_to_multiple(w, 4), pad_to_multiple(h, 4)

        if (new_w, new_h) != (w, h):
            padded = Image.new(img.mode, (new_w, new_h))
            padded.paste(img, (0, 0))
            img = padded

        if has_alpha_result:
            data_bytes = img.tobytes()
            compressed = etcpak.compress_etc2_rgba(data_bytes, new_w, new_h)
            fmt = FORMATS['etc2_rgba8']
        else:
            rgba_img = img.convert('RGBA')
            r, g, b, a = rgba_img.split()
            bgra = Image.merge('RGBA', (b, g, r, a))
            data_bytes = bgra.tobytes()
            compressed = etcpak.compress_etc2_rgb(data_bytes, new_w, new_h)
            fmt = FORMATS['etc2_rgb8']

        write_ktx(output_path, w, h, compressed, fmt)

        if not quiet:
            file_size = os.path.getsize(output_path)
            print(f"✅ 转换成功: {output_path}")
            print(f"   格式: {fmt.name}, 尺寸: {w}×{h}, 大小: {file_size:,} 字节 ({file_size/1024:.1f} KB)")

        return True

    except Exception as e:
        print(f"❌ 转换失败: {e}")
        return False


# ============================================================
#  交互式主菜单
# ============================================================

def main_menu():
    """
    交互式主菜单
    脚本无参数运行时显示
    """
    print()
    print("=" * 60)
    print("🖼️  全格式纹理转换器")
    print("=" * 60)

    # 显示依赖状态
    show_dependency_status(compact=True)

    # 显示支持格式
    print()
    print(f"支持输入: {', '.join(sorted(set(SUPPORTED_INPUT_FORMATS.values())))}")
    print(f"支持输出: ETC2/EAC · BC7/BPTC · S3TC/BC1-BC5 · ASTC · 未压缩")
    print(f"支持 KTX→图片: {', '.join(sorted(set(SUPPORTED_OUTPUT_IMAGE_FORMATS.values())))}")
    print()

    # 菜单选项
    print("请选择操作模式:")
    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │ 转换模式                                    │")
    print("  ├─────────────────────────────────────────────┤")
    print("  │ [1] 🚀 简易模式   快速 PNG→KTX            │")
    print("  │     自动检测透明，自动选择最优 ETC2 格式    │")
    print("  │ [2] 🎨 专家模式   全格式选择                │")
    print("  │     从所有支持的纹理格式中手动选择           │")
    print("  │ [3] 📋 参考模式   从 KTX 匹配格式           │")
    print("  │     读取现有 KTX 的格式，转换新图片         │")
    print("  │ [4] 🖼️  KTX→图片   解压 KTX 为通用图片      │")
    print("  │     支持 ETC2/BC/BC7/ASTC/未压缩 → PNG等    │")
    print("  ├─────────────────────────────────────────────┤")
    print("  │ 工具选项                                    │")
    print("  ├─────────────────────────────────────────────┤")
    print("  │ [5] 📜 格式列表   查看所有支持的格式        │")
    print("  │ [6] 📦 安装依赖   自动安装缺失的依赖        │")
    print("  │ [7] 📋 手动命令   查看手动安装命令          │")
    print("  │ [8] 📊 依赖状态   查看详细依赖状态          │")
    print("  ├─────────────────────────────────────────────┤")
    print("  │ [0] ❌ 退出                                 │")
    print("  └─────────────────────────────────────────────┘")
    print()

    # 获取用户选择
    while True:
        choice = input("请输入选项 (0-8): ").strip()
        if choice in '012345678' and len(choice) == 1:
            break
        print("❌ 无效输入，请输入 0-8")

    # 处理选择
    if choice == '0':
        print("\n👋 感谢使用，再见！")
        sys.exit(0)

    elif choice == '1':
        # 简易模式
        simple_mode()

    elif choice == '2':
        # 专家模式
        expert_mode()

    elif choice == '3':
        # 参考模式
        reference_mode()

    elif choice == '4':
        # KTX → 图片 模式
        ktx_to_image_mode()

    elif choice == '5':
        # 格式列表
        print()
        print("=" * 60)
        print("📜 所有支持的纹理格式")
        print("=" * 60)

        for category_name, category_info in FORMAT_CATEGORIES.items():
            # 检查是否全部不可用
            all_unavailable = all(
                not FORMATS[k].is_available
                for k in category_info['keys']
            )
            status = " ❌ 全部不可用" if all_unavailable else ""

            print(f"\n{'─' * 60}")
            print(f"📦 {category_name}{status}")
            print(f"   {category_info['desc']}")

            if all_unavailable:
                print(f"   → 选择 [6] 安装依赖或 [7] 查看命令")

            print(f"{'─' * 60}")

            for fmt_key in category_info['keys']:
                fmt = FORMATS[fmt_key]
                default_mark = " ⭐" if fmt.default else ""
                available_mark = "" if fmt.is_available else " ❌ 不可用"
                gl_code_hex = f"0x{fmt.gl_code:04X}"

                print(f"  {fmt_key:<25s} | {fmt.name:<35s} | {gl_code_hex} | {fmt.channels}{default_mark}{available_mark}")

            print()

        input("按回车键返回主菜单...")
        print("\n" * 3)
        main_menu()

    elif choice == '6':
        # 安装依赖
        missing = get_missing_dependencies()
        if not missing:
            print("\n✅ 所有依赖已就绪，无需安装")
        else:
            print(f"\n检测到 {len(missing)} 个缺失依赖")
            install_all_dependencies()

        input("\n按回车键返回主菜单...")
        print("\n" * 3)
        main_menu()

    elif choice == '7':
        # 手动安装命令
        print_manual_install_commands()
        input("\n按回车键返回主菜单...")
        print("\n" * 3)
        main_menu()

    elif choice == '8':
        # 依赖状态
        show_dependency_status(compact=False)
        input("按回车键返回主菜单...")
        print("\n" * 3)
        main_menu()


# ============================================================
#  命令行入口
# ============================================================

def main():
    """
    命令行入口点
    支持交互模式和命令行模式
    """
    parser = argparse.ArgumentParser(
        description="🖼️  全格式纹理转换器 - 图片 ↔ KTX 格式双向转换工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  交互模式（无参数）:
    python texture_converter.py

  简易模式 - 快速转换 (图片→KTX):
    python texture_converter.py --simple                        # 交互式
    python texture_converter.py image.png out.ktx --simple      # 自动检测
    python texture_converter.py image.png out.ktx --simple --rgb   # 强制RGB
    python texture_converter.py image.png out.ktx --simple --rgba  # 强制RGBA

  专家模式 - 指定格式 (图片→KTX):
    python texture_converter.py image.png out.ktx -f bc7_srgb
    python texture_converter.py image.png out.ktx -f astc_6x6
    python texture_converter.py image.png out.ktx -f bc3_rgba
    python texture_converter.py image.png out.ktx               # 默认ETC2

  参考模式 - 匹配KTX格式 (图片→KTX):
    python texture_converter.py new.png -r reference.ktx
    python texture_converter.py new.png out.ktx -r reference.ktx

  KTX→图片 (新增):
    python texture_converter.py input.ktx out.png --to-image
    python texture_converter.py input.ktx out.jpg --to-image -q 85
    python texture_converter.py input.ktx --to-image            # 默认PNG

  工具命令:
    python texture_converter.py --list        # 查看所有格式
    python texture_converter.py --install     # 安装依赖
    python texture_converter.py --manual      # 手动安装命令
    python texture_converter.py --status      # 查看依赖状态
    python texture_converter.py --status -v   # 详细依赖状态
        """
    )

    # ---- 位置参数 ----
    parser.add_argument(
        "input", nargs="?",
        help="输入文件路径（图片 或 KTX）"
    )
    parser.add_argument(
        "output", nargs="?",
        help="输出文件路径（可选，默认同目录同名 .ktx 或原扩展名对应图片）"
    )

    # ---- 模式选择 ----
    parser.add_argument(
        "-s", "--simple", action="store_true",
        help="简易模式: 快速 PNG→KTX 转换（原 Sky-png→ktx.py 功能）"
    )
    parser.add_argument(
        "-t", "--to-image", action="store_true",
        help="KTX→图片 模式: 解压 KTX 为通用图片 (PNG/JPG/BMP/WebP/TIFF/TGA)"
    )

    # ---- 格式指定 ----
    parser.add_argument(
        "-f", "--format",
        help="目标纹理格式（专家模式）"
    )
    parser.add_argument(
        "-r", "--reference",
        help="参考 KTX 文件路径（参考模式）"
    )

    # ---- 简易模式选项 ----
    parser.add_argument(
        "--rgb", action="store_true",
        help="[简易模式] 强制使用 RGB（忽略透明通道）"
    )
    parser.add_argument(
        "--rgba", action="store_true",
        help="[简易模式] 强制使用 RGBA（保留透明通道）"
    )

    # ---- KTX→图片 选项 ----
    parser.add_argument(
        "-Q", "--quality", type=int, default=95,
        help="[KTX→图片] JPEG/WebP 输出质量 (0-100，默认 95)"
    )

    # ---- 工具选项 ----
    parser.add_argument(
        "-l", "--list", dest="list_formats", action="store_true",
        help="列出所有支持的纹理格式"
    )
    parser.add_argument(
        "--install", action="store_true",
        help="安装所有缺失的依赖"
    )
    parser.add_argument(
        "--manual", dest="manual_install", action="store_true",
        help="显示手动安装命令"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="显示依赖状态"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="详细模式（配合 --status 使用）"
    )

    # ---- 其他 ----
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="安静模式（减少输出信息）"
    )

    args = parser.parse_args()

    # ========== 工具命令 ==========

    # 列出所有格式
    if args.list_formats:
        print()
        print("=" * 60)
        print("📜 支持的纹理格式完整列表")
        print("=" * 60)

        for category_name, category_info in FORMAT_CATEGORIES.items():
            all_unavailable = all(
                not FORMATS[k].is_available
                for k in category_info['keys']
            )
            status = " ❌ 全部不可用" if all_unavailable else ""

            print(f"\n{'─' * 60}")
            print(f"📦 {category_name}{status}")
            print(f"   {category_info['desc']}")
            print(f"{'─' * 60}")

            for fmt_key in category_info['keys']:
                fmt = FORMATS[fmt_key]
                default_mark = " ⭐" if fmt.default else ""
                available_mark = "" if fmt.is_available else " ❌"
                decode_mark = "" if fmt.is_decodable else " (解码需额外依赖)"
                print(f"  {fmt_key:<25s} → {fmt.name:<35s} (0x{fmt.gl_code:04X}){default_mark}{available_mark}{decode_mark}")
                print(f"  {'':25s}   通道: {fmt.channels}")

            print()

        # 显示当前可用性统计
        available_count = sum(1 for f in FORMATS.values() if f.is_available)
        decodable_count = sum(1 for f in FORMATS.values() if f.is_decodable)
        total_count = len(FORMATS)
        print(f"可压缩格式: {available_count}/{total_count}")
        print(f"可解压格式: {decodable_count}/{total_count}")
        if available_count < total_count or decodable_count < total_count:
            print("→ 使用 --install 安装缺失依赖")

        return

    # 依赖状态
    if args.status:
        show_dependency_status(compact=not args.verbose)
        return

    # 安装依赖
    if args.install:
        missing = get_missing_dependencies()
        if not missing:
            print("✅ 所有依赖已就绪，无需安装")
        else:
            install_all_dependencies()
        return

    # 手动安装命令
    if args.manual_install:
        print_manual_install_commands()
        return

    # ========== 交互模式（无输入参数） ==========
    if not args.input:
        main_menu()
        return

    # ========== KTX → 图片 模式 ==========
    if args.to_image:
        # 输入必须是 .ktx
        if not args.input.lower().endswith('.ktx'):
            print(f"⚠️  输入文件扩展名不是 .ktx: {args.input}")
            confirm = input("   是否仍要尝试？(y/n): ").strip().lower()
            if confirm != 'y':
                sys.exit(1)

        # 确定输出路径
        if args.output:
            output_path = args.output
        else:
            output_path = str(Path(args.input).with_suffix('.png'))

        # 检查输出扩展名
        out_ext = Path(output_path).suffix.lower()
        if out_ext not in SUPPORTED_OUTPUT_IMAGE_FORMATS:
            print(f"⚠️  输出扩展名 {out_ext} 不在支持列表中")
            print(f"   支持的输出格式: {', '.join(sorted(set(SUPPORTED_OUTPUT_IMAGE_FORMATS.values())))}")
            print(f"   → 将自动使用 PNG 格式")
            output_path = str(Path(output_path).with_suffix('.png'))

        success = convert_ktx_to_image(
            args.input, output_path,
            verbose=not args.quiet,
            quality=args.quality
        )
        if not success:
            sys.exit(1)
        return

    # ========== 命令行模式（图片 → KTX） ==========

    # 确定输出路径
    if args.output:
        output_path = args.output
    else:
        output_path = str(Path(args.input).with_suffix('.ktx'))

    # ---- 简易模式 ----
    if args.simple:
        simple_mode_command(
            args.input, output_path,
            force_rgb=args.rgb,
            force_rgba=args.rgba,
            quiet=args.quiet
        )
        return

    # ---- 参考模式 ----
    if args.reference:
        if not os.path.exists(args.reference):
            print(f"❌ 参考 KTX 文件不存在: {args.reference}")
            sys.exit(1)

        print(f"📋 正在分析参考 KTX: {args.reference}")
        ktx_info = analyze_ktx_file(args.reference)

        if not ktx_info:
            print("❌ 无法解析参考 KTX 文件")
            sys.exit(1)

        if not ktx_info['texture_format']:
            print(f"❌ 参考 KTX 的格式 (0x{ktx_info['gl_code']:04X}) 不在支持列表中")
            sys.exit(1)

        texture_format = ktx_info['texture_format']
        print(f"   检测到格式: {texture_format.name} (0x{texture_format.gl_code:04X})")
        print(f"   参考尺寸: {ktx_info['width']} × {ktx_info['height']}")
        print()

    # ---- 手动指定格式 ----
    elif args.format:
        if args.format not in FORMATS:
            print(f"❌ 未知格式: {args.format}")
            print(f"   使用 --list 查看所有支持的格式")
            sys.exit(1)
        texture_format = FORMATS[args.format]

    # ---- 默认格式 ----
    else:
        # 自动选择: 有透明用 RGBA，无透明用 RGB
        try:
            img = Image.open(args.input)
            if has_alpha(img):
                texture_format = FORMATS['etc2_rgba8']
            else:
                texture_format = FORMATS['etc2_rgb8']
        except Exception:
            texture_format = FORMATS['etc2_rgb8']

    # 检查格式可用性
    if not texture_format.is_available:
        missing_parts = []
        if texture_format.requires_s3tc:
            missing_parts.append("Squish 库")
        if texture_format.requires_astc:
            missing_parts.append("ASTC 编码器")
        if texture_format.requires_etcpak:
            missing_parts.append("etcpak 库")

        print(f"❌ 格式 '{texture_format.name}' 不可用")
        print(f"   缺少: {', '.join(missing_parts)}")
        print(f"   运行: python texture_converter.py --install")
        sys.exit(1)

    # 执行转换
    success = convert_image_to_ktx(
        args.input, output_path, texture_format,
        verbose=not args.quiet
    )

    if not success:
        sys.exit(1)


# ============================================================
#  脚本入口
# ============================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  用户中断操作")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 未预期的错误: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)