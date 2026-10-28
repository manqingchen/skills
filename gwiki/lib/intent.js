const WRITE_PATTERNS = [
  /写.{0,20}(需求|prd|spec)/iu,
  /撰写.{0,15}(需求|prd|spec)/iu,
  /起草.{0,15}(需求|prd|spec|文档)/iu,
  /编写.{0,15}(需求|prd|spec|文档)/iu,
  /(?:帮我|来|帮忙|协助).{0,10}(?:写|出|拟|草拟).{0,15}(?:需求|prd|spec)/iu,
  /(?:生成|输出|产出).{0,10}(?:需求|prd|spec)/iu,
  /从.{0,4}(?:零|头|无到有).{0,8}(?:写|拟|做)/u,
  /write.{0,12}(?:a\s+)?(?:requirement|prd|spec|user\s+story)/iu,
  /draft.{0,12}(?:requirement|prd|spec)/iu,
  /create.{0,12}(?:requirement|prd|spec)/iu,
  /新功能.{0,6}(?:需求|prd|spec|方案)/u,
  /需求.{0,4}(?:撰写|编写|起草|文档)/u,
  /requirement-write/iu,
  /gwiki-write/iu,
];

const REVIEW_PATTERNS = [
  /需求评审/u,
  /评审.{0,8}需求/u,
  /review.{0,12}requirement/iu,
  /prd.{0,8}评审/iu,
  /spec.{0,8}评审/iu,
  /看看.{0,6}需求/u,
  /帮我.{0,6}评审/u,
  /这份.{0,6}(需求|prd|spec)/iu,
];

const PLUS_PATTERNS = [
  /怎么实现/u,
  /如何实现/u,
  /代码.{0,6}(在哪|位置|实现)/u,
  /接口.{0,8}(怎么|如何|定义|实现)/u,
  /调用链/u,
  /落地.{0,6}(情况|状态|了吗)/u,
  /实现.{0,6}(了吗|没有|情况)/u,
  /前端.{0,8}(怎么|如何|实现)/u,
  /后端.{0,8}(怎么|如何|实现)/u,
  /wiki\/code/u,
  /\bapi\b/iu,
  /源码/u,
  /类.{0,4}方法/u,
  /业务.{0,4}代码/u,
  /代码证据/u,
  /query-plus/iu,
  /gwiki-plus/iu,
];

const LOCK_PATTERNS = [
  /锁(?:定|到|住|在)?\s*([^\s,，、和与]+(?:\s*[和与、,，]\s*[^\s,，、和与]+)*)/iu,
  /以后.{0,8}(?:都|默认)?\s*(?:用|查|看|走)\s*([^\s,，、和与]+)/iu,
  /固定\s*(?:到|在|用)?\s*([^\s,，、和与]+)/iu,
];

const RELOCK_PATTERNS = [
  /(?:重锁|重新锁|换成|替换锁为|重置锁到)\s*([^\s,，、和与]+(?:\s*[和与、,，]\s*[^\s,，、和与]+)*)/iu,
];

const UNLOCK_PATTERNS = [
  /解锁/u,
  /取消\s*锁定/u,
  /恢复\s*auto/u,
  /释放\s*(?:锁|锁定)/u,
  /暂时\s*解除/u,
];

const PARTIAL_UNLOCK_PATTERNS = [
  /(?:只解锁|从锁定中移除|解锁但保留)\s*([^\s,，、和与]+)/iu,
];

const HARD_UNLOCK_PATTERNS = [
  /(?:彻底|永久|完全|全部)\s*(?:解锁|解除)/u,
  /(?:忘掉|清除|删掉)\s*(?:默认|磁盘)\s*(?:KB|锁)?/u,
];

export function detectGwikiIntent(text, { isCwikiUrl = false } = {}) {
  const source = String(text || "").trim();
  if (!source && !isCwikiUrl) {
    return "query";
  }

  // Lock intents — checked first; lock actions are not questions
  if (RELOCK_PATTERNS.some((pattern) => pattern.test(source))) {
    return "relock-kb";
  }
  if (HARD_UNLOCK_PATTERNS.some((pattern) => pattern.test(source))) {
    return "hard-unlock-kb";
  }
  if (PARTIAL_UNLOCK_PATTERNS.some((pattern) => pattern.test(source))) {
    return "partial-unlock-kb";
  }
  if (UNLOCK_PATTERNS.some((pattern) => pattern.test(source))) {
    return "unlock-kb";
  }
  if (LOCK_PATTERNS.some((pattern) => pattern.test(source))) {
    return "lock-kb";
  }

  // 写意图优先于评审意图（"写需求" vs "评审需求"）
  if (WRITE_PATTERNS.some((pattern) => pattern.test(source))) {
    return "requirement-write";
  }

  if (isCwikiUrl || REVIEW_PATTERNS.some((pattern) => pattern.test(source))) {
    return "requirement-review";
  }

  if (PLUS_PATTERNS.some((pattern) => pattern.test(source))) {
    return "query-plus";
  }

  return "query";
}

const LOCK_INTENTS = new Set([
  "lock-kb",
  "relock-kb",
  "unlock-kb",
  "partial-unlock-kb",
  "hard-unlock-kb",
]);

const INTENT_TO_COMMAND = {
  query: "query",
  "query-plus": "query-plus",
  "requirement-review": "review-requirement",
  "requirement-write": "requirement-write",
  "lock-kb": "lock",
  "relock-kb": "relock",
  "unlock-kb": "unlock",
  "partial-unlock-kb": "partial-unlock",
  "hard-unlock-kb": "unlock",
};

export function commandForIntent(intent) {
  if (intent === "requirement-review") return "review-requirement";
  if (intent === "requirement-write") return "requirement-write";
  if (intent === "query-plus") return "query-plus";
  if (LOCK_INTENTS.has(intent)) return INTENT_TO_COMMAND[intent] || null;
  return "query";
}

export function cloudJobTypeForIntent(intent) {
  // Lock intents don't generate Cloud jobs; signal caller with null
  if (LOCK_INTENTS.has(intent)) {
    return null;
  }
  if (intent === "requirement-review") return "review-requirement";
  // 写意图复用 query job，Cloud 侧走知识库检索提供撰写上下文
  if (intent === "requirement-write") return "query";
  if (intent === "query-plus") return "query-plus";
  return "query";
}
