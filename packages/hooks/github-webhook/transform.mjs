const MAX_MESSAGE_LENGTH = 200;

function isObject(value) {
  return value !== null && typeof value === "object";
}

function normalizeRepo(payload) {
  const repo = payload.repository;
  if (typeof repo === "string" && repo.trim()) {
    return repo.trim();
  }
  if (isObject(repo)) {
    if (typeof repo.full_name === "string" && repo.full_name.trim()) {
      return repo.full_name.trim();
    }
    const name = typeof repo.name === "string" ? repo.name.trim() : "";
    const owner = isObject(repo.owner) && typeof repo.owner.login === "string" ? repo.owner.login.trim() : "";
    if (owner && name) {
      return `${owner}/${name}`;
    }
  }
  return "unknown/repo";
}

function normalizeBranch(ref) {
  if (typeof ref !== "string" || !ref.trim()) {
    return "";
  }
  const trimmed = ref.trim();
  if (!trimmed.includes("/")) {
    return trimmed;
  }
  return trimmed.split("/").pop() || trimmed;
}

function normalizeLine(value, maxLength = MAX_MESSAGE_LENGTH) {
  if (value === null || value === undefined) {
    return "";
  }
  const collapsed = String(value).replace(/\s+/g, " ").trim();
  if (!collapsed) {
    return "";
  }
  if (collapsed.length <= maxLength) {
    return collapsed;
  }
  return `${collapsed.slice(0, maxLength - 3)}...`;
}

function shortSha(sha) {
  if (typeof sha !== "string" || !sha.trim()) {
    return "";
  }
  const trimmed = sha.trim();
  return trimmed.length > 7 ? trimmed.slice(0, 7) : trimmed;
}

function buildSessionKey(repo, sha) {
  const repoName = typeof repo === "string" && repo.includes("/") ? repo.split("/").pop() : repo;
  const short = shortSha(sha);
  if (!repoName || !short) {
    return undefined;
  }
  return `hook:github:${repoName}:${short}`;
}

export function transformGithubWebhook(ctx) {
  const payload = isObject(ctx?.payload) ? ctx.payload : {};
  const headers = isObject(ctx?.headers) ? ctx.headers : {};
  const event = typeof headers["x-github-event"] === "string" ? headers["x-github-event"] : "";

  if (payload.deleted === true) {
    return null;
  }

  const repo = normalizeRepo(payload);

  const isPullRequest = event === "pull_request" || isObject(payload.pull_request);
  if (isPullRequest) {
    const pr = isObject(payload.pull_request) ? payload.pull_request : {};
    const merged = pr.merged === true || Boolean(pr.merged_at);
    const action = typeof payload.action === "string" ? payload.action : "";
    if (!merged || (action && action !== "closed")) {
      return null;
    }

    const number = pr.number ?? payload.number;
    const title = normalizeLine(pr.title);
    const baseRef = normalizeLine(pr.base?.ref);
    const headRef = normalizeLine(pr.head?.ref);
    const sha = pr.merge_commit_sha || pr.head?.sha || payload.sha || payload.after;
    const url = normalizeLine(pr.html_url);
    const author = normalizeLine(pr.user?.login);

    const lines = ["[GitHub Webhook - Repo Update]", "", `Repository: ${repo}`];
    if (number || title) {
      const prLine = `PR: #${number ?? "?"}${title ? ` ${title}` : ""}`.trim();
      lines.push(prLine);
    }
    if (url) {
      lines.push(`URL: ${url}`);
    }
    if (author) {
      lines.push(`Author: ${author}`);
    }
    if (baseRef && headRef) {
      lines.push(`Branch: ${baseRef} <- ${headRef}`);
    } else if (baseRef || headRef) {
      lines.push(`Branch: ${baseRef || headRef}`);
    }
    if (shortSha(sha)) {
      lines.push(`Commit: ${shortSha(sha)}`);
    }
    lines.push("", "Please pull the latest changes and run the test suite.");

    return {
      kind: "agent",
      name: "GitHub",
      sessionKey: buildSessionKey(repo, sha),
      message: lines.join("\n"),
      wakeMode: "now"
    };
  }

  const ref = normalizeLine(payload.ref);
  const branch = normalizeBranch(ref) || normalizeLine(payload.branch);
  const sha = payload.after || payload.sha || payload.head_commit?.id || payload.head_commit?.sha;
  const commitMessage = normalizeLine(payload.message || payload.head_commit?.message || payload.head_commit?.title);
  const url = normalizeLine(payload.compare || payload.head_commit?.url);

  const lines = ["[GitHub Webhook - Repo Update]", "", `Repository: ${repo}`];
  if (branch) {
    lines.push(`Branch: ${branch}`);
  }
  if (shortSha(sha)) {
    lines.push(`Commit: ${shortSha(sha)}`);
  }
  if (commitMessage) {
    lines.push(`Message: \"${commitMessage}\"`);
  }
  if (url) {
    lines.push(`URL: ${url}`);
  }
  lines.push("", "Please pull the latest changes and run the test suite.");

  return {
    kind: "agent",
    name: "GitHub",
    sessionKey: buildSessionKey(repo, sha),
    message: lines.join("\n"),
    wakeMode: "now"
  };
}
