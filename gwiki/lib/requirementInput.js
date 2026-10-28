const CWIKI_HOST_RE = /cwiki\.guazi\.com/i;
const PAGE_ID_FROM_URL_RE = /[?&]pageId=(\d+)/i;

export function parseCwikiPageId(value) {
  const text = String(value || "").trim();
  if (!text) {
    return null;
  }

  const urlMatch = text.match(PAGE_ID_FROM_URL_RE);
  if (urlMatch) {
    return urlMatch[1];
  }

  return null;
}

export function isCwikiUrl(value) {
  const text = String(value || "").trim();
  return CWIKI_HOST_RE.test(text) && Boolean(parseCwikiPageId(text));
}

export function buildReviewRequirementJobInput({ requirement, requirementUrl } = {}) {
  const explicitUrl = String(requirementUrl || "").trim();
  const text = String(requirement || "").trim();

  if (explicitUrl) {
    return { requirementUrl: explicitUrl };
  }

  if (text && isCwikiUrl(text)) {
    return { requirementUrl: text };
  }

  if (text) {
    return { requirement: text };
  }

  throw new Error("requirement text, Cwiki URL, or file path is required");
}
