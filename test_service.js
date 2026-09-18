"use strict";

const { spawnSync } = require("node:child_process");

// 领域纯函数、端到端场景、HTTP API 与原服务契约测试。
const modules = [
  "test_rules",
  "test_competition",
  "test_service_api",
  "service_contract",
];

const result = spawnSync("python3", ["-m", "unittest", "-v", ...modules], {
  stdio: "inherit",
});
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
