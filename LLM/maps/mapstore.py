# -*- coding: utf-8 -*-
r"""
地图文件存取抽象（规格 §B四）——「地图文件在哪、怎么读写」，不含任何 HTTP 与业务校验。

两种实现（对上层接口完全一致，上层代码一行不用改）：
  * ``LocalMapStore`` —— ``pathlib`` 直接读写，**纯 stdlib**；备用形态（后端跑板卡上），
    兼作契约测试的行为基准。
  * ``SshMapStore``   —— SFTP / ``ssh.exe`` 子进程；**主形态**（后端跑 PC 上，地图真相在板卡）。
    两条通道：① paramiko（可选依赖，顶层 try/except）；② ``ssh.exe``/``scp.exe``（零依赖、仅密钥）。

设计要点：
  * **降级不崩**（AGENTS「系统稳健性」）：可用性失败只让 ``available()`` 返回 False 或让
    ``read()`` 抛 ``MapStoreError``，绝不在 import 期硬依赖任何外部包。
  * **离线缓存**（§B4.2）：远程 ``stat()`` 失败且缓存有值时，读路径返回缓存并标注 ``stale``；
    缓存**永不**用于 ``list()`` 的权威结论。
  * **命令注入红线**（§B7.1）：子进程通道由字符串拼命令，``name`` 必须先过
    ``conf.MAP_RE_NAME_RE`` 白名单 —— 本模块自检，不依赖调用方。
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .. import conf

# 允许在 store 里收发的地图扩展名（tags.json 是"标记边车"，与 pgm/yaml 同级同前缀）
EXTS = ("yaml", "pgm", "tags")
_NAME_RE = re.compile(conf.MAP_RE_NAME_RE)


class MapStoreError(RuntimeError):
    """地图文件读写失败（含路径/权限/连接原因，供接口直接透出）。"""


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """把用户给的地图名归一成"纯名字"（可带 .yaml/.pgm/.tags.json 后缀）。

    只做**去后缀**，不做合法性放行 —— 合法性一律走 :func:`check_name`。
    """
    n = (name or "").strip()
    low = n.lower()
    for suf in (".tags.json", ".yaml", ".yml", ".pgm"):
        if low.endswith(suf):
            n = n[: -len(suf)]
            break
    return n


def check_name(name: str) -> str:
    """白名单校验（规格 §B7.1 红线）。返回归一后的名字；不合法抛 ``MapStoreError``。

    拒收：空、超 64 字符、含 ``/`` ``\\`` 空格 引号 ``..``、非 ``[A-Za-z0-9_-]``、含 ``keepout``。
    """
    raw = (name or "").strip()
    n = normalize_name(raw)
    if not n or not _NAME_RE.match(n):
        raise MapStoreError(f"地图名不合法：{raw!r}（只允许字母/数字/下划线/连字符，1~64 字符）")
    if n != raw and ("/" in raw or "\\" in raw):
        # 形如 "../../etc/passwd.pgm" 这类：去后缀后不匹配正则，上面已拦；这里只做双保险
        raise MapStoreError(f"地图名不合法：{raw!r}")
    low = n.lower()
    for bad in conf.MAP_RE_NAME_BANNED:
        if bad in low:
            raise MapStoreError(f"地图名不合法：{raw!r}（不得含 {bad!r}，会被上游编辑器误判为掩膜文件）")
    return n


def _ext_filename(name: str, ext: str) -> str:
    if ext == "yaml":
        return f"{name}.yaml"
    if ext == "pgm":
        return f"{name}.pgm"
    if ext == "tags":
        return f"{name}.tags.json"
    raise MapStoreError(f"不支持的扩展名：{ext!r}（只支持 yaml/pgm/tags）")


# ---------------------------------------------------------------------------
# 缓存（ssh 模式的离线降级，规格 §B4.2）
# ---------------------------------------------------------------------------
class MapCache:
    """``DATA_DIR/mapcache/`` 下的简易 KV 缓存：``{data(base64), mtime, size, at}``。

    键 = ``sha1(io_mode + root + name + ext)``，避免 local/ssh 两套根目录互相污染。
    缓存**只管读**；``list()`` 的权威结论永远来自 store 本身。
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root or conf.MAPS_CACHE_DIR)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception:      # noqa: BLE001  只读文件系统等 → 缓存退化为 no-op
            self.root = None

    def _path(self, key: str) -> Path | None:
        if self.root is None:
            return None
        return self.root / f"{key}.json"

    @staticmethod
    def key(io_mode: str, root: str, name: str, ext: str) -> str:
        raw = f"{io_mode}|{root}|{name}|{ext}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()          # noqa: S324 非安全用途，仅做缓存键

    def get(self, key: str) -> dict | None:
        p = self._path(key)
        if p is None or not p.exists():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            return rec if isinstance(rec, dict) else None
        except Exception:      # noqa: BLE001  坏缓存当没有
            return None

    def put(self, key: str, data: bytes, mtime: float | None, size: int | None) -> None:
        import base64
        p = self._path(key)
        if p is None:
            return
        try:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "data": base64.b64encode(data).decode("ascii"),
                "mtime": mtime, "size": size, "at": time.time(),
            }), encoding="utf-8")
            tmp.replace(p)
        except Exception:      # noqa: BLE001  缓存写失败不影响主流程
            pass

    def data(self, rec: dict) -> bytes:
        import base64
        return base64.b64decode(rec.get("data") or "")

    def drop(self, key: str) -> None:
        p = self._path(key)
        if p is not None and p.exists():
            try:
                p.unlink()
            except Exception:  # noqa: BLE001
                pass

    def clear(self) -> int:
        if self.root is None:
            return 0
        n = 0
        for f in self.root.glob("*.json"):
            try:
                f.unlink()
                n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    def newest_at(self, io_mode: str, root: str, name: str, exts=("yaml", "pgm", "tags")):
        """该图任一扩展名缓存里最新的 ``at`` 时间戳（供"当前离线，显示缓存（时间）"显示）。

        没有任何缓存 → ``None``（此时"离线"这件事由接口返回 ``unavailable`` 表达）。
        """
        newest = None
        for ext in exts:
            rec = self.get(self.key(io_mode, root, name, ext))
            if rec and rec.get("at"):
                newest = max(newest or 0.0, float(rec["at"]))
        return newest


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
class MapStore(Protocol):
    """上层只认这套接口：``maptags`` / ``mapserver`` / 路由都拿它当唯一入口。"""

    io_mode: str
    root: str

    def available(self) -> tuple[bool, str]:
        """(是否可用, 不可用原因)。**不许抛异常**。"""
        ...

    def list(self) -> list[dict]:
        """地图条目列表；``.backup/`` 必须排除。连不上时抛 ``MapStoreError``。"""
        ...

    def read(self, name: str, ext: str) -> bytes: ...
    def write(self, name: str, ext: str, data: bytes) -> None: ...
    def remove(self, name: str, ext: str) -> None: ...
    def rename(self, name: str, new_name: str) -> None: ...
    def copy(self, name: str, new_name: str) -> None: ...
    def stat(self, name: str, ext: str) -> tuple[float, int] | None: ...


# ---------------------------------------------------------------------------
# LocalMapStore —— 纯 stdlib，备用形态 + 契约基准
# ---------------------------------------------------------------------------
class LocalMapStore:
    io_mode = "local"

    def __init__(self, root: str | Path | None = None):
        self.root = str(root if root is not None else conf.MAPS_DIR)
        self._root_path = Path(self.root)
        # 维护用的目录（备份/临时）不该出现在列表里
        self._hidden = {conf.MAPS_BACKUP_DIRNAME}

    # -- 基础 --
    def available(self) -> tuple[bool, str]:
        try:
            if not self._root_path.exists():
                return False, f"本机没有地图目录：{self.root}（请把 MAPS_IO 设为 ssh 或配置 MAPS_DIR）"
            if not self._root_path.is_dir():
                return False, f"地图路径不是目录：{self.root}"
            return True, ""
        except Exception as e:      # noqa: BLE001
            return False, f"地图目录不可访问：{e}"

    def _p(self, name: str, ext: str) -> Path:
        safe = check_name(name)
        return self._root_path / _ext_filename(safe, ext)

    def list(self) -> list[dict]:
        ok, why = self.available()
        if not ok:
            raise MapStoreError(why)
        names: set[str] = set()
        try:
            for f in self._root_path.iterdir():
                if f.is_dir() or f.name in self._hidden:
                    continue
                low = f.name.lower()
                if low.endswith((".pgm", ".yaml", ".yml")):
                    names.add(normalize_name(f.name))
        except Exception as e:      # noqa: BLE001
            raise MapStoreError(f"列目录失败：{e}") from e
        return [self._entry(n) for n in sorted(names)]

    def _entry(self, name: str) -> dict:
        yml = self._root_path / f"{name}.yaml"
        pgm = self._root_path / f"{name}.pgm"
        tags = self._root_path / f"{name}.tags.json"
        item = {
            "name": name, "source": self.io_mode,
            "has_yaml": yml.exists(), "has_pgm": pgm.exists(), "has_tags": tags.exists(),
            "mtime": None, "pgm_size": None,
        }
        mt = 0.0
        for p in (yml, pgm):
            if p.exists():
                try:
                    st = p.stat()
                    mt = max(mt, st.st_mtime)
                    if p is pgm:
                        item["pgm_size"] = st.st_size
                except Exception:   # noqa: BLE001
                    pass
        item["mtime"] = mt or None
        return item

    def read(self, name: str, ext: str) -> bytes:
        p = self._p(name, ext)
        try:
            return p.read_bytes()
        except FileNotFoundError as e:
            raise MapStoreError(f"文件不存在：{p}") from e
        except OSError as e:
            raise MapStoreError(f"读文件失败：{p}（{e}）") from e

    def read_with_meta(self, name: str, ext: str) -> tuple[bytes, bool, float | None]:
        """与 ``SshMapStore`` 同签名：本地读永远不 stale，故第三项恒为 False。"""
        return self.read(name, ext), False, None

    def write(self, name: str, ext: str, data: bytes) -> None:
        p = self._p(name, ext)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)          # 原子替换
        except OSError as e:
            raise MapStoreError(f"写文件失败：{p}（{e}）") from e

    def remove(self, name: str, ext: str) -> None:
        p = self._p(name, ext)
        try:
            if p.exists():
                p.unlink()
        except OSError as e:
            raise MapStoreError(f"删除失败：{p}（{e}）") from e

    def rename(self, name: str, new_name: str) -> None:
        src, dst = check_name(name), check_name(new_name)
        moved = []
        try:
            for ext in EXTS:
                a = self._root_path / _ext_filename(src, ext)
                if a.exists():
                    a.replace(self._root_path / _ext_filename(dst, ext))
                    moved.append(ext)
        except OSError as e:
            raise MapStoreError(f"重命名失败：{src} → {dst}（{e}）") from e
        if not moved:
            raise MapStoreError(f"没有可重命名的文件：{src}")

    def copy(self, name: str, new_name: str) -> None:
        src, dst = check_name(name), check_name(new_name)
        if src == dst:
            raise MapStoreError("目标名与原图相同")
        copied = []
        try:
            for ext in EXTS:
                a = self._root_path / _ext_filename(src, ext)
                b = self._root_path / _ext_filename(dst, ext)
                if a.exists():
                    if b.exists():
                        raise MapStoreError(f"目标已存在：{b}")
                    shutil.copyfile(a, b)
                    copied.append(ext)
        except OSError as e:
            raise MapStoreError(f"复制失败：{src} → {dst}（{e}）") from e
        if not copied:
            raise MapStoreError(f"没有可复制的文件：{src}")

    def stat(self, name: str, ext: str) -> tuple[float, int] | None:
        p = self._p(name, ext)
        try:
            st = p.stat()
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def exists(self, name: str, ext: str) -> bool:
        return self._p(name, ext).exists()

    # -- 备份（规格 §B5.2 第 5 步）--
    def backup(self, name: str, keep: int | None = None) -> list[str]:
        """把该图当前的 pgm+yaml 复制到 ``maps/.backup/<name>.<ts>.{pgm,yaml}``。

        返回写出的备份相对路径列表。保留最近 ``keep`` 组（默认 ``conf.MAPS_BACKUP_KEEP``），
        **只按本模块自己的命名规则删**，绝不递归删目录。
        """
        safe = check_name(name)
        keep = conf.MAPS_BACKUP_KEEP if keep is None else int(keep)
        bdir = self._root_path / conf.MAPS_BACKUP_DIRNAME
        ts = time.strftime("%Y%m%d-%H%M%S")
        made: list[str] = []
        try:
            bdir.mkdir(parents=True, exist_ok=True)
            for ext in ("pgm", "yaml"):
                a = self._root_path / _ext_filename(safe, ext)
                if not a.exists():
                    continue
                b = bdir / f"{safe}.{ts}.{ext}"
                shutil.copyfile(a, b)
                made.append(f"{conf.MAPS_BACKUP_DIRNAME}/{b.name}")
        except OSError as e:
            raise MapStoreError(f"备份失败（{bdir}）：{e}") from e
        self._prune_backups(safe, keep)
        return made

    def _prune_backups(self, name: str, keep: int) -> None:
        bdir = self._root_path / conf.MAPS_BACKUP_DIRNAME
        if not bdir.is_dir():
            return
        groups: dict[str, list[Path]] = {}
        for f in bdir.iterdir():
            if not f.is_file():
                continue                      # 只认平铺文件，目录一概不碰
            if not f.name.startswith(f"{name}."):
                continue
            parts = f.name.split(".")
            # ★ 必须同时要求**扩展名**是 pgm/yaml：只按"前缀 + 点数"匹配的话，
            #   像 `<name>.notes.txt` 这种旁路文件会被算成一组，导致多删一组备份（实测踩过）。
            if len(parts) < 3 or parts[-1] not in ("pgm", "yaml"):
                continue
            groups.setdefault(parts[-2], []).append(f)
        for ts in sorted(groups, reverse=True)[keep:]:
            for f in groups[ts]:
                try:
                    f.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# SshMapStore —— 主形态
# ---------------------------------------------------------------------------
class _CliTransport:
    """``ssh.exe``/``scp.exe`` 子进程通道：零依赖，但**只支持密钥认证**。

    安全：所有拼进命令的 name 都已过 ``check_name`` 白名单；路径用 ``shlex.quote`` 再包一层。
    """

    kind = "cli"

    def __init__(self):
        self.ssh = shutil.which("ssh") or shutil.which("ssh.exe")
        self.scp = shutil.which("scp") or shutil.which("scp.exe")

    def available(self) -> tuple[bool, str]:
        if not self.ssh:
            return False, "找不到 ssh 客户端（Windows 可启用 OpenSSH 客户端）"
        return True, ""

    def _base(self) -> list[str]:
        cmd = [self.ssh, "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={int(conf.MAPS_SSH_TIMEOUT)}",
               "-o", "StrictHostKeyChecking=accept-new",
               "-p", str(conf.MAPS_SSH_PORT)]
        if conf.MAPS_SSH_KEY:
            cmd += ["-i", conf.MAPS_SSH_KEY]
        return cmd

    def _target(self) -> str:
        return f"{conf.MAPS_SSH_USER}@{conf.MAPS_SSH_HOST}"

    def run(self, remote_cmd: str, stdin: bytes | None = None) -> bytes:
        cmd = self._base() + [self._target(), remote_cmd]
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True,
                               timeout=conf.MAPS_SSH_TIMEOUT + 10)
        except subprocess.TimeoutExpired as e:
            raise MapStoreError(f"ssh 超时（{conf.MAPS_SSH_HOST}）：{remote_cmd}") from e
        except OSError as e:
            raise MapStoreError(f"ssh 启动失败：{e}") from e
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace").strip()
            raise MapStoreError(f"ssh 命令失败（exit {p.returncode}）：{err or remote_cmd}")
        return p.stdout or b""

    def read_file(self, remote_path: str) -> bytes:
        return self.run(f"cat {shlex.quote(remote_path)}")

    def write_file(self, remote_path: str, data: bytes, atomic: bool = True) -> None:
        """子进程通道没法"传字节再 mv"，改用 ``sh -c 'cat > tmp && mv tmp dst'``：
        stdin 喂字节，落盘由远端 shell 完成，仍然是"先 tmp 再 mv"的原子替换。"""
        if not atomic:
            self.run(f"sh -c 'cat > {shlex.quote(remote_path)}'", stdin=data)
            return
        tmp = remote_path + ".tmp"
        self.run(f"sh -c 'cat > {shlex.quote(tmp)} && mv {shlex.quote(tmp)} {shlex.quote(remote_path)}'",
                 stdin=data)

    def list_files(self, remote_dir: str) -> list[tuple[str, float, int]]:
        """返回 ``[(文件名, mtime, size), ...]``；``.backup`` 由上层过滤。"""
        out = self.run(
            f"sh -c 'cd {shlex.quote(remote_dir)} 2>/dev/null && "
            f"find . -maxdepth 1 -type f -printf \"%f\\t%T@\\t%s\\n\"'"
        )
        rows: list[tuple[str, float, int]] = []
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, mt, sz = parts
            try:
                rows.append((name, float(mt), int(sz)))
            except ValueError:
                continue
        return rows

    def stat(self, remote_path: str) -> tuple[float, int] | None:
        out = self.run(
            f"sh -c 'stat -c \"%Y\\t%s\" {shlex.quote(remote_path)} 2>/dev/null || true'"
        ).decode("utf-8", "replace").strip()
        if not out:
            return None
        parts = out.split("\t")
        if len(parts) != 2:
            return None
        try:
            return (float(parts[0]), int(parts[1]))
        except ValueError:
            return None

    def mkdirs(self, remote_dir: str) -> None:
        self.run(f"mkdir -p {shlex.quote(remote_dir)}")

    def remove(self, remote_path: str) -> None:
        self.run(f"rm -f {shlex.quote(remote_path)}")

    def copy(self, remote_src: str, remote_dst: str) -> None:
        self.run(f"cp {shlex.quote(remote_src)} {shlex.quote(remote_dst)}")


class _ParamikoTransport:
    """paramiko/SFTP 通道：能流式读写、能吃密码认证。paramiko 是**可选依赖**。"""

    kind = "paramiko"

    def __init__(self):
        import paramiko   # 顶层 try/except 已在 _load_paramiko 做过，这里失败即不可用
        self._paramiko = paramiko
        self._cli = None

    def _client(self):
        if self._cli is None:
            kw = dict(hostname=conf.MAPS_SSH_HOST, port=conf.MAPS_SSH_PORT,
                      username=conf.MAPS_SSH_USER, timeout=conf.MAPS_SSH_TIMEOUT,
                      banner_timeout=conf.MAPS_SSH_TIMEOUT,
                      auth_timeout=conf.MAPS_SSH_TIMEOUT)
            if conf.MAPS_SSH_PASSWORD:
                kw["password"] = conf.MAPS_SSH_PASSWORD
            if conf.MAPS_SSH_KEY:
                kw["key_filename"] = conf.MAPS_SSH_KEY
            c = self._paramiko.SSHClient()
            c.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
            try:
                c.connect(**kw)
            except Exception as e:      # noqa: BLE001
                raise MapStoreError(f"SSH 连接失败（{conf.MAPS_SSH_HOST}:{conf.MAPS_SSH_PORT}）：{e}") from e
            self._cli = c
        return self._cli

    def close(self):
        if self._cli is not None:
            try:
                self._cli.close()
            except Exception:  # noqa: BLE001
                pass
            self._cli = None

    def _sftp(self):
        try:
            return self._client().open_sftp()
        except MapStoreError:
            raise
        except Exception as e:      # noqa: BLE001
            raise MapStoreError(f"打开 SFTP 失败：{e}") from e

    def read_file(self, remote_path: str) -> bytes:
        sf = self._sftp()
        try:
            with sf.open(remote_path, "rb") as f:
                return f.read()
        except FileNotFoundError as e:
            raise MapStoreError(f"远程文件不存在：{remote_path}") from e
        except Exception as e:      # noqa: BLE001
            raise MapStoreError(f"SFTP 读失败：{remote_path}（{e}）") from e
        finally:
            try:
                sf.close()
            except Exception:  # noqa: BLE001
                pass

    def write_file(self, remote_path: str, data: bytes, atomic: bool = True) -> None:
        sf = self._sftp()
        target = remote_path + ".tmp" if atomic else remote_path
        try:
            with sf.open(target, "wb") as f:
                f.write(data)
            if atomic:
                try:
                    sf.remove(remote_path)
                except Exception:   # noqa: BLE001  目标不存在时忽略
                    pass
                sf.rename(target, remote_path)
        except Exception as e:      # noqa: BLE001
            raise MapStoreError(f"SFTP 写失败：{remote_path}（{e}）") from e
        finally:
            try:
                sf.close()
            except Exception:  # noqa: BLE001
                pass

    def _exec(self, cmd: str) -> str:
        try:
            _in, out, err = self._client().exec_command(cmd, timeout=conf.MAPS_SSH_TIMEOUT + 10)
            rc = out.channel.recv_exit_status()
            if rc != 0:
                raise MapStoreError(f"远程命令失败（exit {rc}）：{err.read().decode('utf-8', 'replace').strip()}")
            return out.read().decode("utf-8", "replace")
        except MapStoreError:
            raise
        except Exception as e:      # noqa: BLE001
            raise MapStoreError(f"远程命令异常：{e}") from e

    def list_files(self, remote_dir: str) -> list[tuple[str, float, int]]:
        out = self._exec(
            f"sh -c 'cd {shlex.quote(remote_dir)} 2>/dev/null && "
            f"find . -maxdepth 1 -type f -printf \"%f\\t%T@\\t%s\\n\"'"
        )
        rows: list[tuple[str, float, int]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            try:
                rows.append((parts[0], float(parts[1]), int(parts[2])))
            except ValueError:
                continue
        return rows

    def stat(self, remote_path: str) -> tuple[float, int] | None:
        try:
            st = self._sftp().stat(remote_path)
            return (float(st.st_mtime), int(st.st_size))
        except Exception:      # noqa: BLE001  不存在 → None（与 Local 口径一致）
            return None

    def mkdirs(self, remote_dir: str) -> None:
        self._exec(f"mkdir -p {shlex.quote(remote_dir)}")

    def remove(self, remote_path: str) -> None:
        self._exec(f"rm -f {shlex.quote(remote_path)}")

    def copy(self, remote_src: str, remote_dst: str) -> None:
        self._exec(f"cp {shlex.quote(remote_src)} {shlex.quote(remote_dst)}")


def _paramiko_available() -> tuple[bool, str]:
    try:
        import paramiko   # noqa: F401
        return True, ""
    except Exception as e:      # noqa: BLE001  可选依赖缺失 → 只降级
        return False, f"未安装 paramiko（{type(e).__name__}）"


class SshMapStore:
    """远程读写板卡 maps 目录 + 本地缓存降级（规格 §B3.1 / §B4.2）。"""

    io_mode = "ssh"

    def __init__(self, root: str | None = None, transport: str | None = None,
                 cache: MapCache | None = None):
        self.root = str(root if root is not None else conf.MAPS_SSH_ROOT)
        self.transport_pref = (transport or conf.MAPS_SSH_TRANSPORT or "auto").lower()
        self.cache = cache if cache is not None else MapCache()
        self._tr = None
        self._tr_kind = ""
        self._unavailable = ""
        self._pick_transport()

    # -- 通道选择 --
    def _pick_transport(self):
        pref = self.transport_pref
        pk_ok, pk_why = _paramiko_available()
        if pref in ("auto", "paramiko") and pk_ok:
            try:
                self._tr = _ParamikoTransport()
                self._tr_kind = "paramiko"
                return
            except Exception as e:      # noqa: BLE001
                if pref == "paramiko":
                    self._unavailable = f"paramiko 通道不可用：{e}"
                    return
        if pref in ("auto", "cli"):
            cli = _CliTransport()
            ok, why = cli.available()
            if ok:
                self._tr = cli
                self._tr_kind = "cli"
                return
            self._unavailable = why
            return
        if pref == "paramiko" and not pk_ok:
            self._unavailable = pk_why
            return
        if self._tr is None:
            self._unavailable = self._unavailable or "没有可用的 SSH 通道"

    def available(self) -> tuple[bool, str]:
        if self._tr is None:
            return False, self._unavailable or "没有可用的 SSH 通道（缺 paramiko 且 ssh 客户端不可用）"
        return True, ""

    # -- 路径 --
    def _remote(self, name: str, ext: str) -> str:
        safe = check_name(name)
        base = self.root.rstrip("/")
        return f"{base}/{_ext_filename(safe, ext)}"

    def _key(self, name: str, ext: str) -> str:
        return MapCache.key(self.io_mode, self.root, name, ext)

    # -- 读（带缓存降级）--
    def read_with_meta(self, name: str, ext: str) -> tuple[bytes, bool, float | None]:
        """返回 ``(bytes, stale, cached_at)``。

        * 能连上：stat 与缓存一致 → 用缓存；否则真读并刷新缓存（``stale=False``）。
        * 连不上：有缓存 → 返回缓存并 ``stale=True``；没缓存 → 抛 ``MapStoreError``。
        """
        key = self._key(name, ext)
        rec = self.cache.get(key)
        if self._tr is not None:
            try:
                st = self._tr.stat(self._remote(name, ext))
                if st is not None:
                    if rec and rec.get("mtime") == st[0] and rec.get("size") == st[1]:
                        return self.cache.data(rec), False, None
                    data = self._tr.read_file(self._remote(name, ext))
                    self.cache.put(key, data, st[0], st[1])
                    return data, False, None
                # 文件不存在（远程确认）→ 明确报错，不回退缓存（否则删掉的图还能读出来）
                raise MapStoreError(f"远程文件不存在：{self._remote(name, ext)}")
            except MapStoreError as e:
                if "不存在" in str(e):
                    raise
                if rec is not None:
                    return self.cache.data(rec), True, rec.get("at")
                raise
        if rec is not None:
            return self.cache.data(rec), True, rec.get("at")
        raise MapStoreError(self._unavailable or "地图远程 IO 不可用")

    def read(self, name: str, ext: str) -> bytes:
        data, _stale, _at = self.read_with_meta(name, ext)
        return data

    # -- 写（远程优先，失败不静默重试；同时写穿缓存）--
    def write(self, name: str, ext: str, data: bytes) -> None:
        if self._tr is None:
            raise MapStoreError(self._unavailable or "地图远程 IO 不可用，无法写入")
        path = self._remote(name, ext)
        if self._tr_kind == "cli":
            self._tr.mkdirs(self.root)
        self._tr.write_file(path, data, atomic=True)
        st = None
        try:
            st = self._tr.stat(path)
        except Exception:      # noqa: BLE001
            pass
        self.cache.put(self._key(name, ext), data,
                       st[0] if st else time.time(), st[1] if st else len(data))

    def remove(self, name: str, ext: str) -> None:
        if self._tr is None:
            raise MapStoreError(self._unavailable or "地图远程 IO 不可用，无法删除")
        self._tr.remove(self._remote(name, ext))
        self.cache.drop(self._key(name, ext))

    def rename(self, name: str, new_name: str) -> None:
        if self._tr is None:
            raise MapStoreError(self._unavailable or "地图远程 IO 不可用，无法重命名")
        src, dst = check_name(name), check_name(new_name)
        moved = []
        for ext in EXTS:
            a, b = self._remote(src, ext), self._remote(dst, ext)
            if self._tr.stat(a) is None:
                continue
            if self._tr.stat(b) is not None:
                raise MapStoreError(f"目标已存在：{b}")
            self._tr.copy(a, b)
            self._tr.remove(a)
            self.cache.drop(self._key(src, ext))
            moved.append(ext)
        if not moved:
            raise MapStoreError(f"没有可重命名的文件：{src}")

    def copy(self, name: str, new_name: str) -> None:
        if self._tr is None:
            raise MapStoreError(self._unavailable or "地图远程 IO 不可用，无法复制")
        src, dst = check_name(name), check_name(new_name)
        if src == dst:
            raise MapStoreError("目标名与原图相同")
        copied = []
        for ext in EXTS:
            a, b = self._remote(src, ext), self._remote(dst, ext)
            if self._tr.stat(a) is None:
                continue
            if self._tr.stat(b) is not None:
                raise MapStoreError(f"目标已存在：{b}")
            self._tr.copy(a, b)
            copied.append(ext)
        if not copied:
            raise MapStoreError(f"没有可复制的文件：{src}")

    def stat(self, name: str, ext: str) -> tuple[float, int] | None:
        if self._tr is None:
            return None
        try:
            return self._tr.stat(self._remote(name, ext))
        except MapStoreError:
            return None

    def exists(self, name: str, ext: str) -> bool:
        if self._tr is None:
            return False
        try:
            return self._tr.stat(self._remote(name, ext)) is not None
        except MapStoreError:
            return False

    def list(self) -> list[dict]:
        if self._tr is None:
            raise MapStoreError(self._unavailable or "地图远程 IO 不可用")
        rows = self._tr.list_files(self.root)
        names: set[str] = set()
        meta: dict[str, dict] = {}
        present: set[str] = {r[0] for r in rows}
        backup = conf.MAPS_BACKUP_DIRNAME
        for fname, mt, size in rows:
            if fname.startswith(backup + ".") or fname == backup:
                continue                     # §B9 坑 12：备份目录必须排除
            low = fname.lower()
            if not low.endswith((".pgm", ".yaml", ".yml")):
                continue
            n = normalize_name(fname)
            names.add(n)
            m = meta.setdefault(n, {"mtime": 0.0, "pgm_size": None})
            m["mtime"] = max(m["mtime"], mt)
            if low.endswith(".pgm"):
                m["pgm_size"] = size
        out = []
        for n in sorted(names):
            m = meta[n]
            out.append({"name": n, "source": self.io_mode,
                        "has_yaml": f"{n}.yaml" in present,
                        "has_pgm": f"{n}.pgm" in present,
                        "has_tags": f"{n}.tags.json" in present,
                        "mtime": m["mtime"] or None, "pgm_size": m["pgm_size"]})
        return out

    # -- 备份（远程 shell：cp 到 .backup/ + 按组裁剪）--
    def backup(self, name: str, keep: int | None = None) -> list[str]:
        if self._tr is None:
            raise MapStoreError(self._unavailable or "地图远程 IO 不可用，无法备份")
        safe = check_name(name)
        keep = conf.MAPS_BACKUP_KEEP if keep is None else int(keep)
        bdir = f"{self.root.rstrip('/')}/{conf.MAPS_BACKUP_DIRNAME}"
        self._tr.mkdirs(bdir)
        ts = time.strftime("%Y%m%d-%H%M%S")
        made: list[str] = []
        for ext in ("pgm", "yaml"):
            src = self._remote(safe, ext)
            if self._tr.stat(src) is None:
                continue
            dst = f"{bdir}/{safe}.{ts}.{ext}"
            self._tr.copy(src, dst)
            made.append(f"{conf.MAPS_BACKUP_DIRNAME}/{safe}.{ts}.{ext}")
        self._prune_backups(safe, keep)
        return made

    def _prune_backups(self, name: str, keep: int) -> None:
        """只删 ``<name>.<ts>.pgm|yaml`` 这种平铺文件；不递归、不匹配别的图。"""
        if self._tr is None:
            return
        bdir = f"{self.root.rstrip('/')}/{conf.MAPS_BACKUP_DIRNAME}"
        try:
            rows = self._tr.list_files(bdir)
        except MapStoreError:
            return
        groups: dict[str, list[str]] = {}
        for fname, _mt, _sz in rows:
            if not fname.startswith(f"{name}."):
                continue
            parts = fname.split(".")
            if len(parts) < 3 or parts[-1] not in ("pgm", "yaml"):
                continue
            groups.setdefault(parts[-2], []).append(fname)
        for ts in sorted(groups, reverse=True)[keep:]:
            for fname in groups[ts]:
                try:
                    self._tr.remove(f"{bdir}/{fname}")
                except MapStoreError:
                    pass


# ---------------------------------------------------------------------------
# 工厂（按「地图源」取 store，支持多源共存）
# ---------------------------------------------------------------------------
# 说明（2026-09-14）：原来这里是个"按 conf.MAPS_IO 造一个单例"的工厂，地图源等于进程级隐式状态。
# 现在改成**按命名源**造实例并各自缓存 —— 但**默认路径完全兼容**：不传 source_id 时走
# mapsources 的默认源，而默认源的首个种子就是从 conf.MAPS_IO / MAPS_DIR / MAPS_SSH_* 来的。
_stores: dict[tuple, MapStore] = {}      # 缓存键 = 源的指纹（路径/主机变了自然不命中）
_last_resolve: dict = {}                 # 最近一次解析结果：{"id","label","kind","root","warnings"}


def _build(io_mode: str, root: str, ssh: dict | None = None) -> MapStore:
    """按配置造一个 store（不缓存）。"""
    if io_mode == "local":
        return LocalMapStore(root=root)
    ssh = ssh or {}
    # SshMapStore 读的是 conf 里的连接参数；这里为"某一条 ssh 源"临时覆盖，用完还原，
    # 免得把进程级配置改脏（多源共存时这很关键）。
    keys = ("MAPS_SSH_HOST", "MAPS_SSH_USER", "MAPS_SSH_PORT", "MAPS_SSH_ROOT", "MAPS_SSH_KEY")
    backup = {k: getattr(conf, k) for k in keys}
    try:
        if ssh.get("host"):
            conf.MAPS_SSH_HOST = str(ssh["host"])
        if ssh.get("user"):
            conf.MAPS_SSH_USER = str(ssh["user"])
        if ssh.get("port"):
            conf.MAPS_SSH_PORT = int(ssh["port"])
        if ssh.get("root"):
            conf.MAPS_SSH_ROOT = str(ssh["root"])
        if "key" in ssh:
            conf.MAPS_SSH_KEY = str(ssh.get("key") or "")
        return SshMapStore(root=str(ssh.get("root") or conf.MAPS_SSH_ROOT))
    finally:
        for k, v in backup.items():
            setattr(conf, k, v)


def resolve_source(source_id: str = "") -> dict:
    """把 ``source_id``（可空=默认源）解析成源字典。空/未知都给出明确信息。"""
    from . import mapsources      # 延迟导入：mapsources 是上层（只依赖 conf），避免 import 期成环
    src = mapsources.get(source_id)
    _last_resolve.clear()
    _last_resolve.update({"id": src["id"], "label": src.get("label", src["id"]),
                          "kind": src["kind"], "root": src.get("root", "")})
    return src


def get_store(source_id: str = "", force_new: bool = False) -> MapStore:
    """按地图源取 ``MapStore``（每个源一个实例，按源指纹缓存）。

    * ``source_id`` 空 → 用 ``mapsources`` 的**默认源**（首次运行由 ``conf.MAPS_IO`` 种入）；
    * 源不存在 → 抛 ``MapStoreError``（路由层转 400/404，附可用源清单）；
    * 源配置改了（换目录/换主机）→ 指纹变，自动重建，无需重启后端。
    """
    from . import mapsources
    src = resolve_source(source_id)
    fp = mapsources.fingerprint_of(src)
    key = (fp["io_mode"], fp["root"], fp.get("host", ""), fp.get("user", ""), fp.get("port", ""))
    if force_new or key not in _stores:
        _stores[key] = _build(fp["io_mode"], fp["root"], src if fp["io_mode"] == "ssh" else None)
    return _stores[key]


def current_source() -> dict:
    """最近一次 :func:`get_store`/``resolve_source`` 解析出的源（给 io_status 显示用）。"""
    if _last_resolve:
        return dict(_last_resolve)
    try:
        return resolve_source()
    except Exception:      # noqa: BLE001  源表都没了也不该让状态接口崩
        return {"id": "", "label": "", "kind": conf.MAPS_IO, "root": str(conf.MAPS_DIR)}


def reset_store() -> None:
    """丢弃全部缓存的 store（测试与源配置变更后调用）。"""
    _stores.clear()


def io_status(name: str = "", source_id: str = "") -> dict:
    """``GET /api/mapeditor/io`` 的载荷（规格 §B5.2）。

    ``name`` 非空时额外给出该图的缓存新鲜度：连不上/读失败但缓存里有值 → ``stale: True``
    + ``cached_at``（前端据此显示「当前离线，显示缓存（2026-09-14 11:00）」）。
    """
    st = get_store(source_id)
    src = current_source()
    ok, why = st.available()
    out = {
        # source 是新口径（哪个命名源）；mode/root/host 保留给老前端与日志，语义不变
        "source": src.get("id", ""),
        "source_label": src.get("label", ""),
        "source_kind": src.get("kind", ""),
        "mode": "local" if src.get("kind") == "local" else "ssh",
        "root": st.root,
        "available": ok,
        "reason": why,
        "transport": getattr(st, "_tr_kind", "") or ("local" if src.get("kind") == "local" else ""),
        "stale": False,
        "cached_at": None,
        "host": conf.MAPS_SSH_HOST if src.get("kind") == "ssh" else "",
    }
    cache = getattr(st, "cache", None)
    if name and cache is not None:
        at = cache.newest_at(st.io_mode, st.root, normalize_name(name))
        if at:
            out["cached_at"] = at
            out["cached_at_text"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))
            if not ok:
                out["stale"] = True
    return out


def io_test(source_id: str = "") -> dict:
    """主动连通性自检：只 ``list()`` 一次，返回耗时与错误原因；**不改任何文件**。"""
    st = get_store(source_id)
    src = current_source()
    t0 = time.time()
    ok, why = st.available()
    base = {"source": src.get("id", ""), "source_label": src.get("label", ""),
            "root": st.root, "elapsed_ms": int((time.time() - t0) * 1000)}
    if not ok:
        return {"ok": False, "available": False, "reason": why, "count": 0, **base}
    try:
        items = st.list()
    except MapStoreError as e:
        return {"ok": False, "available": True, "reason": str(e), "count": 0, **base}
    return {"ok": True, "available": True, "reason": "", "count": len(items),
            "transport": getattr(st, "_tr_kind", ""),
            "elapsed_ms": int((time.time() - t0) * 1000), **base}
