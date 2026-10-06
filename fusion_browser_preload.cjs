"use strict";
const childProcess = require("child_process");
const path = require("path");

let extra = [];
try {
  const parsed = JSON.parse(process.env.FUSION_CHROMIUM_ARGS || "[]");
  if (Array.isArray(parsed)) extra = parsed.filter((arg) => typeof arg === "string" && arg);
} catch (_) {
  extra = [];
}

const CHROMIUM = /chrom|headless.shell|msedge/i;

function withArgs(command, args) {
  if (!extra.length || typeof command !== "string" || !Array.isArray(args)) return args;
  if (!CHROMIUM.test(path.basename(command))) return args;
  if (!args.some((arg) => typeof arg === "string" && arg.startsWith("--remote-debugging-"))) return args;
  return [...args, ...extra.filter((arg) => !args.includes(arg))];
}

if (extra.length) {
  const spawn = childProcess.spawn;
  childProcess.spawn = function (command, args, ...rest) {
    return spawn.call(this, command, withArgs(command, args), ...rest);
  };
  require("module").syncBuiltinESMExports();
}

module.exports = { withArgs };
