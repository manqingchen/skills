const CWIKI_PAGE_URL = "https://cwiki.guazi.com/pages/viewpage.action?pageId=";
const CWIKI_MARKDOWN_LINK_RE = /\[([^\]]+)\]\((https:\/\/cwiki\.guazi\.com\/pages\/viewpage\.action\?pageId=(\d+)[^)]+)\)/g;
const MAX_KEY_SOURCE_LINKS = 5;
const MAX_INLINE_SOURCE_LINKS = 3;

const SOURCE_SLUG_RE = /^(?:wiki\/sources\/|sources\/)?(\d+)-(.+?)(?:-index)?(?:\.md)?$/;
const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;
const BACKTICK_WIKI_PATH_RE = /`((?:wiki|raw|raw-code)\/[^`]+)`/g;
const PLAIN_WIKI_SOURCE_RE = /(?<![(\[`])(?:wiki\/sources\/)(\d+-[^\s`，。；;：:）)\]"']+?-index\.md)/g;

const SUMMARY_INCLUDE_HEADINGS = [
  /^查询类型$/,
  /^知识库$/,
  /^结论(?:[:：].*)?$/,
  /^关键来源$/,
  /^未决(?:点)?(?:\s*\/\s*证据缺口|\s*\/\s*缺口)?(?:[:：].*)?$/,
  /^证据缺口(?:[:：].*)?$/,
  /^建议下一步$/,
];

const SUMMARY_EXCLUDE_HEADINGS = [
  /^检索路径$/,
  /^证据(?:路径)?$/,
  /^业务\s*\/\s*需求证据$/,
  /^实现核验$/,
  /^Source Metadata$/,
];

export function cwikiUrlFromPageId(pageId) {
  const normalized = String(pageId || "").trim();
  if (!/^\d+$/u.test(normalized)) {
    return null;
  }
  return `${CWIKI_PAGE_URL}${normalized}`;
}

export function titleFromSourceSlug(slugOrPath) {
  const normalized = String(slugOrPath || "")
    .replace(/^wiki\/sources\//, "")
    .replace(/^sources\//, "")
    .replace(/-index\.md$/u, "")
    .replace(/\.md$/u, "");
  const match = normalized.match(SOURCE_SLUG_RE);
  if (!match) {
    return normalized.replace(/^wiki\//, "").replace(/\//g, " / ");
  }
  return match[2].replace(/-/g, " ").trim() || normalized;
}

export function markdownLink(label, pageId) {
  const url = cwikiUrlFromPageId(pageId);
  const text = String(label || "").trim() || titleFromSourceSlug(pageId);
  if (!url) {
    return text;
  }
  return `[${text}](${url})`;
}

export function formatEvidenceLinks(markdown) {
  let text = String(markdown || "");

  text = text.replace(WIKILINK_RE, (full, target, label) => {
    const pageId = extractPageIdFromTarget(target);
    if (pageId) {
      return markdownLink(label || titleFromSourceSlug(target), pageId);
    }
    const plainLabel = String(label || target)
      .replace(/^concepts\//, "")
      .replace(/^entities\//, "")
      .replace(/^wiki\//, "")
      .replace(/\/index$/, "")
      .replace(/-/g, " ")
      .trim();
    return plainLabel || full;
  });

  text = text.replace(BACKTICK_WIKI_PATH_RE, (_full, pathValue) => {
    const pageId = extractPageIdFromTarget(pathValue);
    if (pageId) {
      return markdownLink(titleFromSourceSlug(pathValue), pageId);
    }
    if (/^wiki\/(?:concepts|entities|overview)/u.test(pathValue)) {
      return titleFromSourceSlug(pathValue);
    }
    if (/^wiki\/code\//u.test(pathValue)) {
      const tail = pathValue.split("/").pop()?.replace(/\.md$/u, "").replace(/-/g, " ") || "代码证据";
      return tail;
    }
    return titleFromSourceSlug(pathValue);
  });

  text = text.replace(PLAIN_WIKI_SOURCE_RE, (full) => {
    const pageId = extractPageIdFromTarget(full);
    if (!pageId) {
      return full;
    }
    return markdownLink(titleFromSourceSlug(full), pageId);
  });

  return text;
}

export function ensureKbSection(markdown, kbContext = {}) {
  const text = String(markdown || "");
  if (/^#{1,3}\s*知识库/m.test(text)) {
    return text;
  }
  const block = buildKbSection(kbContext);
  if (!block) {
    return text;
  }
  const queryTypeMatch = text.match(/^##\s*查询类型[^\n]*\n(?:.*\n)*?(?=^##\s|\Z)/m);
  if (queryTypeMatch) {
    const insertAt = queryTypeMatch.index + queryTypeMatch[0].length;
    return `${text.slice(0, insertAt).trimEnd()}\n\n${block}\n${text.slice(insertAt)}`;
  }
  return `${block}\n\n${text}`.trim();
}

export function ensureUnresolvedSection(markdown) {
  const text = String(markdown || "");
  if (/^#{1,3}\s*(未决点|未决|证据缺口)/m.test(text)) {
    return text;
  }
  const nextStepsMatch = text.match(/^##\s*建议下一步/m);
  const section = "## 未决点 / 证据缺口\n\n- 无（本次回答未列出未决点）\n";
  if (nextStepsMatch) {
    const insertAt = nextStepsMatch.index;
    return `${text.slice(0, insertAt).trimEnd()}\n\n${section}\n${text.slice(insertAt)}`;
  }
  return `${text.trimEnd()}\n\n${section}`.trim();
}

export function formatQueryAnswer({ markdown, kb = null, kbs = null } = {}) {
  let text = String(markdown || "");
  text = formatEvidenceLinks(text);
  text = ensureKbSection(text, normalizeKbContext({ kb, kbs }));
  text = ensureUnresolvedSection(text);
  text = ensureConclusionSourceLine(text);
  text = ensureKeySourcesSection(text);
  text = moveSectionToEnd(text, /^##\s*检索路径[^\n]*$/m);
  return text.trim();
}

export function summarizeMarkdownForDisplay(markdown) {
  const formatted = formatQueryAnswer({ markdown });
  const lines = formatted.split(/\r?\n/);
  const output = [];
  let currentHeading = null;
  let includeCurrent = true;

  for (const line of lines) {
    const headingMatch = line.match(/^#{1,3}\s+(.+?)\s*$/);
    if (headingMatch) {
      currentHeading = normalizeHeading(headingMatch[1]);
      includeCurrent = shouldIncludeHeading(currentHeading);
      if (includeCurrent) {
        output.push(line);
      }
      continue;
    }
    if (includeCurrent) {
      output.push(line);
    }
  }

  const summary = output.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  if (summary) {
    return summary;
  }
  return formatted.split(/\r?\n/).slice(0, 20).join("\n").trim();
}

function normalizeKbContext({ kb, kbs } = {}) {
  if (Array.isArray(kbs) && kbs.length > 0) {
    return { kbs: kbs.map(normalizeKbEntry).filter(Boolean) };
  }
  const single = normalizeKbEntry(kb);
  return single ? { kb: single } : {};
}

function normalizeKbEntry(entry) {
  if (!entry || typeof entry !== "object") {
    return null;
  }
  const id = String(entry.id || entry.kbId || "").trim();
  if (!id) {
    return null;
  }
  return {
    id,
    displayName: String(entry.displayName || entry.display_name || id).trim(),
    description: cleanDescription(entry.description || entry.summary || ""),
  };
}

function buildKbSection(kbContext) {
  const entries = Array.isArray(kbContext.kbs) && kbContext.kbs.length > 0
    ? kbContext.kbs
    : kbContext.kb
      ? [kbContext.kb]
      : [];
  if (entries.length === 0) {
    return "";
  }
  const lines = entries.map((entry) => {
    const domain = entry.description ? `：${entry.description}` : "";
    return `- **${entry.displayName}**${domain}`;
  });
  return ["## 知识库", "", ...lines].join("\n");
}

function cleanDescription(value) {
  return String(value || "")
    .replace(/^>\s*/, "")
    .replace(/\[\[([^|\]]+\|)?([^\]]+)\]\]/g, "$2")
    .replace(/`/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function ensureConclusionSourceLine(markdown) {
  const text = String(markdown || "");
  const links = extractCwikiMarkdownLinks(text).slice(0, MAX_INLINE_SOURCE_LINKS);
  if (links.length === 0) {
    return text;
  }

  const conclusionMatch = text.match(/^##\s*结论[^\n]*\n[\s\S]*?(?=^##\s|\Z)/m);
  if (!conclusionMatch) {
    return text;
  }

  const conclusionSection = conclusionMatch[0];
  if (/关键来源[:：]/.test(conclusionSection) || hasCwikiMarkdownLink(conclusionSection)) {
    return text;
  }

  const line = `关键来源：${formatInlineSourceLinks(links)}`;
  const updatedSection = `${conclusionSection.trimEnd()}\n\n${line}\n`;
  return `${text.slice(0, conclusionMatch.index)}${updatedSection}${text.slice(conclusionMatch.index + conclusionSection.length)}`;
}

function ensureKeySourcesSection(markdown) {
  const text = String(markdown || "");
  if (/^#{1,3}\s*关键来源/m.test(text)) {
    return text;
  }

  const links = extractCwikiMarkdownLinks(text).slice(0, MAX_KEY_SOURCE_LINKS);
  if (links.length === 0) {
    return text;
  }

  const section = [
    "## 关键来源",
    "",
    ...links.map((link) => `- [${link.label}](${link.url})`),
    "",
  ].join("\n");

  const unresolvedMatch = text.match(/^##\s*(未决点|未决|证据缺口)/m);
  if (unresolvedMatch) {
    return `${text.slice(0, unresolvedMatch.index).trimEnd()}\n\n${section}${text.slice(unresolvedMatch.index)}`;
  }

  const nextStepsMatch = text.match(/^##\s*建议下一步/m);
  if (nextStepsMatch) {
    return `${text.slice(0, nextStepsMatch.index).trimEnd()}\n\n${section}${text.slice(nextStepsMatch.index)}`;
  }

  return `${text.trimEnd()}\n\n${section}`.trim();
}

function extractCwikiMarkdownLinks(markdown) {
  const links = [];
  const seenPageIds = new Set();
  for (const match of String(markdown || "").matchAll(CWIKI_MARKDOWN_LINK_RE)) {
    const [, label, url, pageId] = match;
    if (!pageId || seenPageIds.has(pageId)) {
      continue;
    }
    seenPageIds.add(pageId);
    links.push({
      label: String(label || "").trim() || pageId,
      url,
      pageId,
    });
  }
  return links;
}

function hasCwikiMarkdownLink(markdown) {
  return /\[[^\]]+\]\(https:\/\/cwiki\.guazi\.com\/pages\/viewpage\.action\?pageId=\d+[^\)]*\)/.test(String(markdown || ""));
}

function formatInlineSourceLinks(links) {
  return links
    .map((link) => `[${link.label}](${link.url})`)
    .join("；");
}

function extractPageIdFromTarget(target) {
  const normalized = String(target || "").trim();
  const match = normalized.match(/(?:^|\/)(\d+)-/u);
  return match?.[1] || null;
}

function normalizeHeading(value) {
  return String(value || "")
    .replace(/^#+\s*/, "")
    .replace(/[:：].*$/, "")
    .trim();
}

function shouldIncludeHeading(heading) {
  if (SUMMARY_EXCLUDE_HEADINGS.some((pattern) => pattern.test(heading))) {
    return false;
  }
  if (SUMMARY_INCLUDE_HEADINGS.some((pattern) => pattern.test(heading))) {
    return true;
  }
  if (/^结论/.test(heading)) {
    return true;
  }
  if (/^未决/.test(heading) || /^证据缺口/.test(heading)) {
    return true;
  }
  return false;
}

function moveSectionToEnd(markdown, headingPattern) {
  const text = String(markdown || "");
  const sectionMatch = text.match(new RegExp(`${headingPattern.source}\\n[\\s\\S]*?(?=^##\\s|\\Z)`, "m"));
  if (!sectionMatch) {
    return text;
  }
  const section = sectionMatch[0].trim();
  const withoutSection = `${text.slice(0, sectionMatch.index)}${text.slice(sectionMatch.index + sectionMatch[0].length)}`
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (!withoutSection) {
    return section;
  }
  return `${withoutSection}\n\n${section}\n`;
}
