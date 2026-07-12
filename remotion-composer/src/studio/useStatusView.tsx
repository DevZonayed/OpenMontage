// Single source of the canonical /status view for the whole Studio page.
//
// Both the dominant CommandCenter card and the compact Production inspector
// consume THIS one controller, so they can never disagree on stage/headline/owner
// and there is only ever ONE production poll + ONE primary action across the page.
// Network errors preserve the last known state ("reconnecting") — never fabricate.

import { useCallback, useEffect, useRef, useState } from "react";
import { BacklotClient } from "../composition/client";
import { StatusAction, StatusView } from "../composition/status";

function pollMs(v: StatusView | null): number {
  const st = v?.overall_state;
  if (st === "producing" || st === "planning" || st === "cancelling") return 2500;
  if (st === "reconciling") return 2000;
  if (["awaiting_approval", "awaiting_plan_approval", "blocked", "ready_to_produce"].includes(st ?? "")) return 4000;
  return 8000;
}

export interface StatusController {
  view: StatusView | null;
  coldError: boolean;
  busy: boolean;
  actionError: string | null;
  connectOpen: boolean;
  setConnectOpen: (b: boolean) => void;
  refresh: () => void;
  runAction: (a: StatusAction) => void;
}

export function useStatusView(client: BacklotClient): StatusController {
  const [view, setView] = useState<StatusView | null>(null);
  const [coldError, setColdError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [connectOpen, setConnectOpen] = useState(false);
  const lastGood = useRef<StatusView | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const tick = useCallback(async () => {
    try {
      const v = await client.getStatus();
      lastGood.current = v;
      setColdError(false);
      setView(v);
    } catch {
      if (lastGood.current) {
        setView({
          ...lastGood.current,
          stale: true,
          diagnostics: [
            ...(lastGood.current.diagnostics || []),
            { kind: "stale", message: "Reconnecting to live updates…" },
          ],
        });
      } else {
        setColdError(true);
      }
    }
  }, [client]);

  useEffect(() => {
    let alive = true;
    const loop = async () => {
      if (!alive) return;
      await tick();
      if (!alive) return;
      timer.current = setTimeout(loop, lastGood.current ? pollMs(lastGood.current) : 3000);
    };
    void loop();
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [tick]);

  const refresh = useCallback(() => { void tick(); }, [tick]);

  const runAction = useCallback(async (a: StatusAction) => {
    setActionError(null);
    const P = view?.run_id ?? undefined;
    const brainRun = !!view?.sources?.brain_run_id;
    try {
      switch (a.id) {
        case "connect_hermes":
        case "retry_connect":
          setConnectOpen(true);
          return;
        case "start":
        case "continue_hermes":
        case "restart":
          setBusy(true);
          await client.startRun();
          break;
        case "approve_plan":
          setBusy(true);
          await client.approvePlan(P ?? "");
          break;
        case "request_changes":
          if (!window.confirm("Send the plan back for changes? The current run stops; nothing is lost.")) return;
          setBusy(true);
          await client.cancelCoarseRun(P ?? "");
          break;
        case "preview":
          setBusy(true);
          await client.previewRun();
          break;
        case "view_deliverable":
          return;
        case "approve":
          setBusy(true);
          await client.approveRun(P ?? "", { approvalId: a.approval_id ?? undefined, stage: a.stage ?? undefined });
          break;
        case "reject":
          setBusy(true);
          await client.rejectRun(P ?? "", { approvalId: a.approval_id ?? undefined, stage: a.stage ?? undefined });
          break;
        case "retry_stage":
        case "retry_control":
          setBusy(true);
          await client.retryStage(a.stage ?? view?.current_stage ?? "", P);
          break;
        case "stop":
          if (!window.confirm("Stop this production run? Completed work is preserved.")) return;
          setBusy(true);
          if (brainRun) await client.cancelRun(P ?? "");
          else await client.cancelCoarseRun(P ?? "");
          break;
        default:
          return;
      }
      await tick();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [client, tick, view]);

  return { view, coldError, busy, actionError, connectOpen, setConnectOpen, refresh, runAction };
}
