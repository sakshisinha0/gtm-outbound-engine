#!/usr/bin/env node
/**
 * Multi-board job scraper for the GTM Outbound Engine.
 *
 * Pulls jobs that match icp_config.json from several job boards and writes one CSV
 * with the columns: job board, title, company name, job link, company domain.
 *
 *   hiringcafe  Browser scrape with Playwright (the site renders in JavaScript).
 *               The company domain comes straight from the job card.
 *   himalayas   Public API. No domain in the data, so that column is left blank.
 *   yc          Work at a Startup. The domain comes from the company's YC page.
 *
 * Filtering (title include/exclude, locations, max age) follows the same rules as
 * job_signals.py. This file is self-contained: it needs Node 18+ and nothing else
 * except Playwright for HiringCafe.
 *
 * HiringCafe sits behind Cloudflare, which blocks headless browsers, so a browser
 * window opens for that board by default. If it shows "Performing security
 * verification", tick the checkbox and the script carries on. The browser profile is saved in
 * .hiringcafe-profile/, so later runs usually skip the check. Pass --headless to hide
 * the window (it will likely be blocked).
 *
 * Setup (HiringCafe only):
 *     npm install playwright
 *     npx playwright install chromium
 *
 * Usage:
 *     node multi_board_scrape.js
 *     node multi_board_scrape.js --boards hiringcafe,yc
 *     node multi_board_scrape.js --config icp_config.json --out board_jobs.csv --headless
 */

import fs from "node:fs";

const BOARDS = ["hiringcafe", "himalayas", "yc"];
const CSV_COLUMNS = ["job board", "title", "company name", "job link", "company domain"];

const USER_AGENT = "Mozilla/5.0 (compatible; job-signals-script/2.0)";
const TIMEOUT_MS = 20000;
const REQUEST_DELAY_MS = 1000;
const HIMALAYAS_MAX_PAGES = 3;
const DEFAULT_YC_ROLES = ["marketing", "operations", "sales-manager"];

const HIRINGCAFE_NAV_TIMEOUT_MS = 45000;
const HIRINGCAFE_CARD_TIMEOUT_MS = 20000;
const HIRINGCAFE_PROFILE_DIR = ".hiringcafe-profile";
const HIRINGCAFE_VERIFY_TIMEOUT_MS = 180000; // time to tick Cloudflare's checkbox by hand
const HIRINGCAFE_SCROLLS = 4;
const WORKPLACES = new Set(["remote", "hybrid", "onsite", "field"]);
const DAY_MS = 86400000;

// ---------------------------------------------------------------------------
// Fetching and parsing helpers
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** GET a URL and return the body as text. Retries once on a network error. */
async function fetchText(url) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const response = await fetch(url, {
        headers: { "User-Agent": USER_AGENT },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (response.status === 404) throw new Error("page not found");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.text();
    } catch (e) {
      lastError = e;
      if (e.message === "page not found") break;
    }
    if (attempt === 0) await sleep(2000);
  }
  throw lastError;
}

/** Be polite to real servers; tests can pass a fake fetch to skip the wait. */
async function pause(fetcher) {
  if (fetcher === fetchText) await sleep(REQUEST_DELAY_MS);
}

const NAMED_ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

function unescapeHtml(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, body) => {
    if (body[0] === "#") {
      const code = body[1].toLowerCase() === "x" ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return Number.isFinite(code) && code <= 0x10ffff ? String.fromCodePoint(code) : whole;
    }
    return NAMED_ENTITIES[body.toLowerCase()] ?? whole;
  });
}

/** Unescape, strip tags, unescape again (some sources double-escape). */
function stripHtml(text) {
  if (!text) return "";
  text = unescapeHtml(text).replace(/<[^>]+>/g, " ");
  return unescapeHtml(text).replace(/\s+/g, " ").trim();
}

/** Accept ISO strings, or epoch timestamps in seconds or milliseconds. */
function parseDate(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string" && /^\d+$/.test(value.trim())) value = Number(value);
  const date = typeof value === "number" ? new Date(value > 1e11 ? value : value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function domainOf(website) {
  if (!website) return "";
  try {
    const host = new URL(website.includes("//") ? website : "//" + website, "https://x").hostname;
    return host.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** The JSON a YC page embeds as an escaped data-page attribute. */
function ycPageProps(pageHtml) {
  const match = pageHtml.match(/data-page="([^"]+)"/);
  if (!match) return null;
  return JSON.parse(unescapeHtml(match[1])).props ?? {};
}

// ---------------------------------------------------------------------------
// Board: HiringCafe (Playwright)
// ---------------------------------------------------------------------------

function hiringcafeUrl(keyword, maxAgeDays) {
  const state = JSON.stringify({ searchQuery: keyword, dateFetchedPastNDays: maxAgeDays });
  return "https://hiringcafe.com/classic?searchState=" + encodeURIComponent(state);
}

/** Runs in the page: one object per job card. */
function readHiringcafeCards(cards) {
  return cards.map((c) => {
    const job = c.querySelector('a[href*="/job/"]');
    const org = c.querySelector('a[href*="/org/"]');
    return {
      title: c.querySelector("span.font-bold")?.innerText || "",
      company: c.querySelector("img")?.alt || "",
      link: job?.href || "",
      domain: org ? new URL(org.href).pathname.split("/org/")[1] : "",
      lines: c.innerText.split("\n").map((s) => s.trim()).filter(Boolean),
    };
  });
}

/** Card text reads: age, title, location, workplace, ... */
function normalizeHiringcafe(card) {
  const at = card.lines.indexOf(card.title);
  const after = at >= 0 ? card.lines.slice(at + 1) : [];
  return {
    board: "HiringCafe",
    company: card.company,
    title: card.title,
    location: after[0] ?? "",
    workplace: after.slice(0, 3).map((l) => l.toLowerCase()).find((l) => WORKPLACES.has(l)) ?? "",
    date: null, // the search is already limited to the max age
    url: card.link,
    domain: card.domain,
    description: "",
  };
}

/** Cloudflare may show "Performing security verification": wait while the user ticks the box. */
async function waitForCards(page) {
  const cards = "[data-quick-job-card]";
  try {
    await page.waitForSelector(cards, { timeout: HIRINGCAFE_CARD_TIMEOUT_MS });
  } catch {
    console.log("HiringCafe is showing a security check. Tick the checkbox in the browser window...");
    await page.waitForSelector(cards, { timeout: HIRINGCAFE_VERIFY_TIMEOUT_MS });
  }
}

async function pullHiringcafe(keywords, maxAgeDays, headless = false) {
  let chromium;
  try {
    ({ chromium } = await import("playwright"));
  } catch {
    return [[], ["hiringcafe: playwright not installed (npm install playwright && npx playwright install chromium)"]];
  }

  const jobs = [];
  const errors = [];
  // A persistent profile keeps Cloudflare's clearance, so the check is only needed once.
  const browser = await chromium.launchPersistentContext(HIRINGCAFE_PROFILE_DIR, { headless });
  try {
    const page = browser.pages()[0] ?? await browser.newPage();
    for (const keyword of keywords) {
      try {
        await page.goto(hiringcafeUrl(keyword, maxAgeDays), {
          waitUntil: "domcontentloaded",
          timeout: HIRINGCAFE_NAV_TIMEOUT_MS,
        });
        await waitForCards(page);
        for (let i = 0; i < HIRINGCAFE_SCROLLS; i++) { // cards load as you scroll
          await page.mouse.wheel(0, 4000);
          await page.waitForTimeout(1000);
        }
        const cards = await page.$$eval("[data-quick-job-card]", readHiringcafeCards);
        jobs.push(...cards.filter((c) => c.link).map(normalizeHiringcafe));
      } catch (e) { // one bad keyword shouldn't stop the run
        errors.push(`hiringcafe '${keyword}': ${String(e.message).split("\n")[0]}`);
      }
    }
  } finally {
    await browser.close();
  }
  return [jobs, errors];
}

// ---------------------------------------------------------------------------
// Board: Himalayas (https://himalayas.app/api)
// ---------------------------------------------------------------------------

function himalayasUrl(keyword, page) {
  const query = new URLSearchParams({ q: keyword, sort: "recent", page: String(page) });
  return `https://himalayas.app/jobs/api/search?${query}`;
}

function normalizeHimalayas(data) {
  return (data.jobs ?? []).map((job) => {
    const countries = job.locationRestrictions || [];
    return {
      board: "Himalayas",
      company: job.companyName || "",
      title: job.title || "",
      location: countries.length ? countries.join(", ") : "Worldwide",
      // All Himalayas jobs are remote, but a country list means only people in those
      // countries can take it, so only worldwide jobs count as "remote".
      workplace: countries.length ? "" : "remote",
      date: parseDate(job.pubDate),
      url: job.applicationLink || job.guid || "",
      domain: "",
      description: stripHtml(job.description),
    };
  });
}

/** Search each title keyword. Results are newest first, so stop paging at old posts. */
async function pullHimalayas(keywords, fetcher, maxAgeDays, now) {
  const jobs = [];
  const errors = [];
  const cutoff = new Date(now.getTime() - maxAgeDays * DAY_MS);
  for (const keyword of keywords) {
    for (let page = 1; page <= HIMALAYAS_MAX_PAGES; page++) {
      let batch;
      try {
        await pause(fetcher);
        batch = normalizeHimalayas(JSON.parse(await fetcher(himalayasUrl(keyword, page))));
      } catch (e) { // one bad query shouldn't stop the run
        errors.push(`himalayas '${keyword}' page ${page}: ${e.message}`);
        break;
      }
      if (!batch.length) break;
      jobs.push(...batch);
      if (batch.every((j) => j.date && j.date < cutoff)) break;
    }
  }
  return [jobs, errors];
}

// ---------------------------------------------------------------------------
// Board: Y Combinator Work at a Startup (https://www.ycombinator.com/jobs)
// ---------------------------------------------------------------------------

const ycUrl = (role) => `https://www.ycombinator.com/jobs/role/${encodeURIComponent(role)}`;

/** Turn YC's '8 months' / 'over 1 year' / 'about 3 hours' into an approximate date. */
function relativeAge(text, now) {
  const match = (text || "").toLowerCase().match(/(\d+|a|an)\s+(minute|hour|day|week|month|year)/);
  if (!match) return null;
  const amount = match[1] === "a" || match[1] === "an" ? 1 : Number(match[1]);
  const days = { minute: 0, hour: 0, day: 1, week: 7, month: 30, year: 365 }[match[2]];
  return new Date(now.getTime() - amount * days * DAY_MS);
}

function normalizeYc(pageHtml, now) {
  const props = ycPageProps(pageHtml);
  if (!props) throw new Error("YC page format changed (no data-page found)");
  return (props.jobPostings ?? []).map((job) => {
    const location = job.location || "";
    return {
      board: "Y Combinator",
      company: job.companyName || "",
      title: job.title || "",
      location,
      workplace: location.toLowerCase().includes("remote") ? "remote" : "",
      date: relativeAge(job.createdAt, now), // YC only says "8 months", so this is approximate
      url: job.url ? "https://www.ycombinator.com" + job.url : "",
      domain: "",
      // The list page has no job text, so YC jobs rarely get tool mentions.
      description: job.companyOneLiner || "",
    };
  });
}

async function pullYc(roles, fetcher, now) {
  const jobs = [];
  const errors = [];
  for (const role of roles) {
    try {
      await pause(fetcher);
      jobs.push(...normalizeYc(await fetcher(ycUrl(role)), now));
    } catch (e) {
      errors.push(`yc '${role}': ${e.message}`);
    }
  }
  return [jobs, errors];
}

/** https://www.ycombinator.com/companies/omnistrate/jobs/... -> .../companies/omnistrate */
function ycCompanyUrl(jobUrl) {
  return jobUrl.match(/^https:\/\/www\.ycombinator\.com\/companies\/[^/?#]+/)?.[0] ?? "";
}

function websiteFromYcPage(pageHtml) {
  return ycPageProps(pageHtml)?.company?.website || "";
}

/** YC job pages link to a company page that lists the website (cached per company). */
async function fillYcDomains(jobs, fetcher = fetchText) {
  const cache = new Map();
  for (const job of jobs) {
    if (job.board !== "Y Combinator") continue;
    if (!cache.has(job.company)) {
      let website = "";
      try {
        await pause(fetcher);
        const pageUrl = ycCompanyUrl(job.url);
        website = pageUrl ? websiteFromYcPage(await fetcher(pageUrl)) : "";
      } catch {
        website = "";
      }
      cache.set(job.company, domainOf(website));
    }
    job.domain = cache.get(job.company);
  }
}

// ---------------------------------------------------------------------------
// Matching jobs against the ICP
// ---------------------------------------------------------------------------

const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Match a keyword as a whole word, so 'intern' doesn't match 'internal'. */
const keywordPattern = (keyword) =>
  new RegExp(`(?<![a-z0-9])${escapeRegex(keyword.toLowerCase())}(?![a-z0-9])`);

function compileRules(config) {
  const keywords = (key) => (config[key] ?? []).filter((k) => k.trim());
  const compiled = (key) => keywords(key).map((k) => [k, keywordPattern(k)]);
  return {
    maxAgeDays: config.max_age_days ?? 30,
    titleKeywords: keywords("title_include"),
    ycRoles: config.yc_roles ?? DEFAULT_YC_ROLES,
    titleInclude: compiled("title_include"),
    titleExclude: compiled("title_exclude"),
    locationsInclude: compiled("locations_include"),
  };
}

const found = (patterns, text) => {
  text = text.toLowerCase();
  return patterns.filter(([, pattern]) => pattern.test(text)).map(([keyword]) => keyword);
};

function matchesIcp(job, rules, now) {
  if (!found(rules.titleInclude, job.title).length) return false;
  if (found(rules.titleExclude, job.title).length) return false;
  if (rules.locationsInclude.length
      && !found(rules.locationsInclude, `${job.location} ${job.workplace}`).length) return false;
  if (job.date) {
    const daysOld = Math.max(0, Math.floor((now - job.date) / DAY_MS));
    if (daysOld > rules.maxAgeDays) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Running it
// ---------------------------------------------------------------------------

const csvCell = (value) => {
  const text = String(value ?? "");
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
};

async function run(configPath, outPath, boards, headless = false, now = new Date()) {
  const rules = compileRules(JSON.parse(fs.readFileSync(configPath, "utf-8")));

  const raw = [];
  const errors = [];
  const collect = ([jobs, errs]) => { raw.push(...jobs); errors.push(...errs); };
  if (boards.includes("hiringcafe")) collect(await pullHiringcafe(rules.titleKeywords, rules.maxAgeDays, headless));
  if (boards.includes("himalayas")) collect(await pullHimalayas(rules.titleKeywords, fetchText, rules.maxAgeDays, now));
  if (boards.includes("yc")) collect(await pullYc(rules.ycRoles, fetchText, now));

  const rows = [];
  const seen = new Set();
  for (const job of raw) {
    if (!job.url || seen.has(job.url)) continue;
    if (matchesIcp(job, rules, now)) {
      seen.add(job.url);
      rows.push(job);
    }
  }

  if (boards.includes("yc")) await fillYcDomains(rows);

  const lines = [CSV_COLUMNS, ...rows.map((j) => [j.board, j.title, j.company, j.url, j.domain])]
    .map((row) => row.map(csvCell).join(","));
  fs.writeFileSync(outPath, "﻿" + lines.join("\r\n") + "\r\n", "utf-8");

  const byBoard = {};
  for (const j of rows) byBoard[j.board] = (byBoard[j.board] ?? 0) + 1;
  console.log(`Scraped ${raw.length} jobs, kept ${rows.length} that match the ICP: ${JSON.stringify(byBoard)}`);
  console.log(`Blank domains: ${rows.filter((j) => !j.domain).length} (fill them with company_domains.py)`);
  console.log(`Wrote ${outPath}`);
  for (const error of errors) console.error(`  warning: ${error}`);
  return { rows, errors };
}

function parseArgs(argv) {
  const args = { config: "icp_config.json", out: "board_jobs.csv", boards: BOARDS.join(","), headless: false };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--headless") args.headless = true;
    else if (["--config", "--out", "--boards"].includes(flag) && i + 1 < argv.length) args[flag.slice(2)] = argv[++i];
    else fail(`unrecognized argument: ${flag}`);
  }
  return args;
}

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(2);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const boards = args.boards.split(",").map((b) => b.trim().toLowerCase()).filter(Boolean);
  const unknown = boards.filter((b) => !BOARDS.includes(b));
  if (unknown.length) fail(`unknown board(s): ${unknown.join(", ")}`);
  await run(args.config, args.out, boards, args.headless);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
