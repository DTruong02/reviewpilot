"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, api, type Page, type PullRequest, type Repository } from "@/lib/api";
import { Notice, PageHeader, Panel, RepoLink, Status } from "@/components/ui";

type DashboardData = {
  repositories: Page<Repository>;
  pullRequests: PullRequest[];
};

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const repositories = await api.repositories(0, 10);
        const pages = await Promise.all(
          repositories.items.map((repository) => api.pullRequests(repository.id, 0, 5)),
        );
        const pullRequests = pages.flatMap((page) => page.items)
          .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
        if (active) setData({ repositories, pullRequests });
      } catch (cause) {
        if (active) setError(cause instanceof ApiError ? cause.message : "Could not load dashboard.");
      }
    }
    void load();
    return () => { active = false; };
  }, []);

  const statuses = data?.pullRequests.reduce<Record<string, number>>((counts, pullRequest) => {
    const status = pullRequest.latest_review_status ?? "not reviewed";
    counts[status] = (counts[status] ?? 0) + 1;
    return counts;
  }, {}) ?? {};

  return <>
    <PageHeader eyebrow="WORKSPACE / OVERVIEW" title="Your review desk"
      subtitle="A calm view of repositories connected to ReviewPilot."
      action={<Link className="button primary" href="/repositories">Browse repositories ↗</Link>} />
    {error && <Notice kind="error">{error}</Notice>}
    <div className="stat-grid">
      <div className="stat-card"><small>CONNECTED REPOSITORIES</small><strong>{data?.repositories.total ?? "—"}</strong><span>Available to review</span></div>
      <div className="stat-card"><small>QUEUED / RUNNING</small><strong>{(statuses.queued ?? 0) + (statuses.running ?? 0)}</strong><span>Among the latest pull requests</span></div>
      <div className="stat-card"><small>COMPLETED / FAILED</small><strong>{(statuses.completed ?? 0) + (statuses.failed ?? 0)}</strong><span>{statuses.completed ?? 0} completed · {statuses.failed ?? 0} failed</span></div>
    </div>
    <Panel title="Recent pull requests" aside={<span className="text-muted">Latest 5 per repository · up to 10 repositories</span>}>
      {error ? null : !data ? <Notice kind="loading">Loading recent activity…</Notice>
        : data.pullRequests.length === 0 ? <Notice kind="empty">No pull requests yet. Accepted pull request webhooks will add activity here.</Notice>
          : <div className="list">{data.pullRequests.slice(0, 12).map((pullRequest) => <Link className="list-row" href={`/pull-requests/${pullRequest.id}`} key={pullRequest.id}>
            <div><b>#{pullRequest.number} {pullRequest.title}</b><small>{pullRequest.repository_full_name} · Updated {new Date(pullRequest.updated_at).toLocaleDateString()}</small></div>
            <Status value={pullRequest.latest_review_status} /><span aria-hidden="true">↗</span>
          </Link>)}</div>}
    </Panel>
    <Panel title="Repositories" aside={<Link className="text-link" href="/repositories">View all →</Link>}>
      {error ? null : !data ? <Notice kind="loading">Loading repositories…</Notice>
        : data.repositories.items.length === 0 ? <Notice kind="empty">No repositories yet.</Notice>
          : <div className="repo-grid">{data.repositories.items.slice(0, 6).map((repository) => <article className="repo-card" key={repository.id}>
            <div className="repo-symbol">{repository.name.slice(0, 1).toUpperCase()}</div>
            <div className="repo-info"><RepoLink id={repository.id}>{repository.full_name}</RepoLink><small>Updated · {new Date(repository.updated_at).toLocaleDateString()}</small></div>
            <Status value="connected" /><Link className="card-arrow" aria-label={`Open ${repository.full_name}`} href={`/repositories/${repository.id}`}>↗</Link>
          </article>)}</div>}
    </Panel>
  </>;
}
