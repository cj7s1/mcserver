import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { makeInstance } from "@/test/fixtures";
import type { Instance } from "@/lib/types";
import { BackupNotice, BackupWarningBadge } from "./BackupNotice";

const NOW = Date.parse("2026-11-09T12:00:00Z");
const ID = "11111111-1111-1111-1111-111111111111";
const NINE_DAYS_AGO = "2026-10-31T11:00:00Z";

function server(over: Partial<Instance> = {}): Instance {
  return makeInstance({
    id: ID,
    permissions: ["instance.view", "instance.backup", "instance.schedules"],
    last_backup_at: null,
    backup_schedules: 0,
    ...over,
  });
}

function show(instance: Instance, backupsLink = true) {
  return render(
    <MemoryRouter>
      <BackupNotice instance={instance} backupsLink={backupsLink} now={NOW} />
      <BackupWarningBadge instance={instance} now={NOW} />
    </MemoryRouter>,
  );
}

describe("BackupNotice", () => {
  it("tells that a server was never backed up and points to Backups and the Nightly backup template", () => {
    show(server());
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent("No backup yet");
    expect(notice).toHaveTextContent(/has no backup schedule/);
    expect(screen.getByRole("link", { name: "Open Backups" })).toHaveAttribute("href", `/servers/${ID}/backups`);
    expect(screen.getByRole("link", { name: "Schedules" })).toHaveAttribute("href", `/servers/${ID}/schedules`);
    expect(notice).toHaveTextContent(/“Nightly backup” template/);
  });

  it("gives the age of an old backup and no template pointer when a schedule exists", () => {
    show(server({ last_backup_at: NINE_DAYS_AGO, backup_schedules: 1 }));
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent("Last backup 9 days ago");
    expect(notice).not.toHaveTextContent(/Nightly backup/);
    expect(screen.getByRole("link", { name: "Check Schedules" })).toHaveAttribute("href", `/servers/${ID}/schedules`);
  });

  it("keeps the first-backup case calm when a schedule is already in place", () => {
    show(server({ backup_schedules: 1 }));
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(/schedule takes the first backup on its next run/);
    expect(screen.queryByRole("link", { name: /Schedules/ })).toBeNull();
  });

  it("does not link to the Backups page from the Backups page", () => {
    show(server(), false);
    expect(screen.queryByRole("link", { name: "Open Backups" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent(/Create backup/);
    expect(screen.getByRole("link", { name: "Schedules" })).toBeInTheDocument();
  });

  it("does not offer the template to someone who cannot manage schedules", () => {
    show(server({ permissions: ["instance.view", "instance.backup"] }));
    const notice = screen.getByRole("status");
    expect(notice).not.toHaveTextContent(/template/);
    expect(notice).toHaveTextContent(/someone who may manage schedules/);
  });

  it.each([
    ["a recent backup", { last_backup_at: "2026-11-08T12:00:00Z" }],
    ["an archived server", { archived_at: "2026-11-01T00:00:00Z" }],
    ["a viewer without the backup permission", { permissions: ["instance.view", "instance.console"] }],
  ] as [string, Partial<Instance>][])("renders nothing for %s", (_name, over) => {
    const { container } = show(server(over));
    expect(container).toBeEmptyDOMElement();
  });
});

describe("BackupWarningBadge", () => {
  it("is a small link to the server's Backups page", () => {
    show(server({ last_backup_at: NINE_DAYS_AGO }));
    const badge = screen.getByRole("link", { name: /Backup 9 days old/ });
    expect(badge).toHaveAttribute("href", `/servers/${ID}/backups`);
    expect(badge).toHaveAttribute("title", "Last backup 9 days ago. No backup schedule is enabled. Open Backups.");
    expect(badge).toHaveAttribute("data-slot", "badge");
  });

  it("names a never backed up server", () => {
    show(server({ backup_schedules: 1 }));
    expect(screen.getByRole("link", { name: /No backup yet/ })).toHaveAttribute(
      "title",
      "No backup yet. A backup schedule is enabled. Open Backups.",
    );
  });
});
