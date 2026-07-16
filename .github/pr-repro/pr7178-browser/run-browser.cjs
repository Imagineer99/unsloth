const { Builder, Browser } = require("selenium-webdriver");
const chrome = require("selenium-webdriver/chrome");
const edge = require("selenium-webdriver/edge");
const firefox = require("selenium-webdriver/firefox");
const safari = require("selenium-webdriver/safari");

const browser = process.argv[2];
const url = process.argv[3] || "http://127.0.0.1:4174";

function configureBuilder(name) {
  const builder = new Builder();
  if (name === "chrome") {
    return builder
      .forBrowser(Browser.CHROME)
      .setChromeOptions(new chrome.Options().addArguments("--headless=new", "--disable-gpu"));
  }
  if (name === "edge") {
    return builder
      .forBrowser(Browser.EDGE)
      .setEdgeOptions(new edge.Options().addArguments("--headless=new", "--disable-gpu"));
  }
  if (name === "firefox") {
    return builder
      .forBrowser(Browser.FIREFOX)
      .setFirefoxOptions(new firefox.Options().addArguments("-headless"));
  }
  if (name === "safari") return builder.forBrowser(Browser.SAFARI).setSafariOptions(new safari.Options());
  throw new Error(`Unsupported browser: ${name}`);
}

(async () => {
  const driver = await configureBuilder(browser).build();
  try {
    await driver.get(url);
    const results = await driver.wait(
      () => driver.executeScript("return window.__securityResults || null"),
      30_000,
      "security harness did not finish",
    );
    console.log(JSON.stringify({ browser, ...results }, null, 2));
    if (results.failed !== 0 || results.passed !== 9) process.exitCode = 1;
  } finally {
    await driver.quit();
  }
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
