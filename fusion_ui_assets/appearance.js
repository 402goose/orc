/* Apply saved appearance before CSS paints; preferences belong to this browser. */
"use strict";
(() => {
  const key = "orc-appearance";
  const themes = [
    {
      id: "grove",
      name: "Grove",
      hue: 105,
      saturation: 12,
      dark: "#c2f277",
      light: "#416912",
    },
    {
      id: "ocean",
      name: "Ocean",
      hue: 215,
      saturation: 24,
      dark: "#8cc9ff",
      light: "#1764a4",
    },
    {
      id: "iris",
      name: "Iris",
      hue: 265,
      saturation: 20,
      dark: "#c6adff",
      light: "#7343ba",
    },
    {
      id: "rose",
      name: "Rose",
      hue: 335,
      saturation: 18,
      dark: "#f5a9ce",
      light: "#a12f69",
    },
    {
      id: "amber",
      name: "Amber",
      hue: 38,
      saturation: 20,
      dark: "#f3ce7b",
      light: "#855910",
    },
    {
      id: "glacier",
      name: "Glacier",
      hue: 190,
      saturation: 22,
      dark: "#86dce9",
      light: "#126a7b",
    },
    {
      id: "mint",
      name: "Mint",
      hue: 160,
      saturation: 18,
      dark: "#8be0bd",
      light: "#167052",
    },
    {
      id: "sand",
      name: "Sand",
      hue: 28,
      saturation: 14,
      dark: "#dfc5a2",
      light: "#7c5834",
    },
    {
      id: "slate",
      name: "Slate",
      hue: 220,
      saturation: 6,
      dark: "#c0cbdc",
      light: "#4b5e7c",
    },
    {
      id: "ember",
      name: "Ember",
      hue: 14,
      saturation: 18,
      dark: "#ffb193",
      light: "#a64122",
    },
  ];
  const modes = ["light", "dark", "system"];
  const system = matchMedia("(prefers-color-scheme: dark)");
  function normalize(value) {
    return {
      theme: themes.some((t) => t.id === value?.theme) ? value.theme : "grove",
      mode: modes.includes(value?.mode) ? value.mode : "system",
    };
  }
  function read() {
    try {
      return normalize(JSON.parse(localStorage.getItem(key)));
    } catch {
      return normalize(null);
    }
  }
  let current = read();
  function apply() {
    const theme = themes.find((t) => t.id === current.theme);
    const dark =
      current.mode === "system" ? system.matches : current.mode === "dark";
    const root = document.documentElement;
    const surface = (lightness) =>
      `hsl(${theme.hue} ${theme.saturation}% ${lightness}%)`;
    const values = {
      bg: surface(dark ? 7 : 97),
      panel: surface(dark ? 11 : 100),
      panel2: surface(dark ? 14 : 94),
      sidebar: surface(dark ? 9 : 96),
      input: surface(dark ? 8 : 100),
      code: surface(dark ? 6 : 95),
      line: surface(dark ? 23 : 82),
      muted: surface(dark ? 66 : 37),
      text: surface(dark ? 94 : 13),
      lime: dark ? theme.dark : theme.light,
      "on-accent": dark ? surface(8) : "#ffffff",
      purple: dark ? "#c4b3ee" : "#6946a1",
      red: dark ? "#f1a599" : "#b3352f",
      amber: dark ? "#e9c47b" : "#82570d",
      green: dark ? "#bbdc91" : "#42701e",
      backdrop: dark ? "#00000099" : "#19223255",
    };
    for (const [name, value] of Object.entries(values))
      root.style.setProperty("--" + name, value);
    root.dataset.theme = current.theme;
    root.dataset.mode = dark ? "dark" : "light";
    root.style.colorScheme = dark ? "dark" : "light";
    document.dispatchEvent(
      new CustomEvent("orc-appearance-change", { detail: { ...current } }),
    );
  }
  window.ORCAppearance = {
    themes,
    get: () => ({ ...current }),
    set(value) {
      current = normalize({ ...current, ...value });
      try {
        localStorage.setItem(key, JSON.stringify(current));
      } catch {
        /* Restricted storage still permits an in-tab preview. */
      }
      apply();
    },
  };
  system.addEventListener("change", () => {
    if (current.mode === "system") apply();
  });
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) {
      current = read();
      apply();
    }
  });
  apply();
})();
