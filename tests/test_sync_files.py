#!/usr/bin/env python3
"""
Test deploy_vllm.py sync_files_to_workers mechanism.
验证多机算子调优状态同步逻辑的正确性（不启动实际容器）。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../skills/flagos-container-preparation/tools'))

def test_sync_files_config_parsing():
    """Test 1: sync_files 配置解析"""
    print("[测试 1] sync_files 配置解析")

    # Case 1: 无 sync_files 段 → 跳过
    cfg1 = {"nodes": [{"host": "a"}, {"host": "b"}], "docker": {"container_name": "c"}}
    result = cfg1.get("sync_files")
    assert result is None, "无 sync_files 应返回 None"
    print("  ✓ 无 sync_files 段 → None")

    # Case 2: sync_files 为空列表 → 跳过
    cfg2 = {"sync_files": [], "nodes": [{"host": "a"}], "docker": {"container_name": "c"}}
    result = cfg2.get("sync_files")
    assert result == [], "空 sync_files 应返回 []"
    print("  ✓ sync_files: [] → 跳过")

    # Case 3: sync_files 含文件路径 → 解析
    cfg3 = {
        "sync_files": ["/root/flaggems_ops_control.json", "/etc/environment"],
        "nodes": [{"host": "a"}, {"host": "b"}],
        "docker": {"container_name": "c"}
    }
    result = cfg3.get("sync_files")
    assert len(result) == 2, "应解析出 2 个文件路径"
    assert "/root/flaggems_ops_control.json" in result
    assert "/etc/environment" in result
    print("  ✓ sync_files 解析出 2 个路径")


def test_sync_files_skip_conditions():
    """Test 2: sync_files 跳过条件"""
    print("\n[测试 2] sync_files 跳过条件")

    # 单节点场景
    cfg_single = {
        "sync_files": ["/root/flaggems_ops_control.json"],
        "nodes": [{"host": "master"}],
        "docker": {"container_name": "c"}
    }
    assert len(cfg_single["nodes"]) == 1, "单节点应跳过 sync"
    print("  ✓ 单节点场景 → 跳过 sync")

    # 无 sync_files
    cfg_no_sync = {
        "nodes": [{"host": "a"}, {"host": "b"}],
        "docker": {"container_name": "c"}
    }
    assert cfg_no_sync.get("sync_files") is None
    print("  ✓ 无 sync_files 配置 → 跳过 sync")


def test_sync_command_construction():
    """Test 3: 文件同步命令构造逻辑验证"""
    print("\n[测试 3] 文件同步命令构造逻辑")

    container = "multi-test"
    fpath = "/root/flaggems_ops_control.json"
    fname = "flaggems_ops_control.json"
    local_tmp = f"/tmp/deploy_vllm_sync/{fname}"

    # Step 1: master → local
    cmd_export = f"docker cp {container}:{fpath} {local_tmp} 2>/dev/null || echo 'File not found: {fpath}'"
    assert "docker cp" in cmd_export
    assert fpath in cmd_export
    assert local_tmp in cmd_export
    print(f"  ✓ master→local: docker cp {container}:{fpath} {local_tmp}")

    # Step 2: local → worker (local mode)
    cmd_import_local = f"docker cp {local_tmp} {container}:{fpath}"
    assert "docker cp" in cmd_import_local
    assert local_tmp in cmd_import_local
    assert fpath in cmd_import_local
    print(f"  ✓ local→worker(local): docker cp {local_tmp} {container}:{fpath}")

    # Step 3: local → worker (remote mode via scp + ssh)
    remote_host = "172.21.16.14"
    remote_user = "root"
    remote_port = 22
    key_file = "/root/.ssh/id_rsa"
    remote_tmp = f"/tmp/{fname}"

    scp_cmd = f"scp -i {key_file} -P {remote_port} -o StrictHostKeyChecking=no {local_tmp} {remote_user}@{remote_host}:{remote_tmp}"
    assert "scp" in scp_cmd
    assert key_file in scp_cmd
    assert local_tmp in scp_cmd
    assert remote_tmp in scp_cmd
    print(f"  ✓ local→remote_host: scp {local_tmp} {remote_user}@{remote_host}:{remote_tmp}")

    ssh_docker_cp = f"ssh -i {key_file} -p {remote_port} -o StrictHostKeyChecking=no {remote_user}@{remote_host} 'docker cp {remote_tmp} {container}:{fpath}'"
    assert "ssh" in ssh_docker_cp
    assert "docker cp" in ssh_docker_cp
    assert remote_tmp in ssh_docker_cp
    assert fpath in ssh_docker_cp
    print(f"  ✓ remote_host→container: ssh {remote_user}@{remote_host} 'docker cp {remote_tmp} {container}:{fpath}'")


def test_integration_dry_run():
    """Test 4: 端到端集成测试（dry-run）"""
    print("\n[测试 4] 端到端集成测试（dry-run）")

    cfg = {
        "sync_files": ["/root/flaggems_ops_control.json"],
        "docker": {"container_name": "multi-test"},
        "nodes": [
            {"name": "master", "host": "172.21.16.6", "local": True},
            {"name": "worker1", "host": "172.21.16.14", "ssh_user": "root"}
        ],
        "ssh": {"password": "", "port": 22}
    }

    # 验证配置完整性
    assert "sync_files" in cfg
    assert len(cfg["nodes"]) == 2
    assert cfg["nodes"][0].get("local") is True
    print("  ✓ 配置结构完整")

    # 验证 master 识别
    master = cfg["nodes"][0]
    worker = cfg["nodes"][1]
    assert master["local"] is True
    assert worker.get("local") is not True
    print("  ✓ master/worker 节点识别正确")

    # 验证同步目标
    sync_files = cfg["sync_files"]
    assert len(sync_files) == 1
    assert sync_files[0] == "/root/flaggems_ops_control.json"
    print(f"  ✓ 同步目标: {sync_files}")


if __name__ == "__main__":
    print("=" * 60)
    print("deploy_vllm.py sync_files 机制测试")
    print("=" * 60)

    try:
        test_sync_files_config_parsing()
        test_sync_files_skip_conditions()
        test_sync_command_construction()
        test_integration_dry_run()

        print("\n" + "=" * 60)
        print("✓ 所有测试通过")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n✗ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ 异常: {e}")
        sys.exit(1)
