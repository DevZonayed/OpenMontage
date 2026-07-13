// Learned Style-preference types for the Studio Style panel.
// Mirrors backlot/preferences_api.py (read_preferences / update_preference).
// There is NO production-run / agent state here — OpenMontage is manual-first.

export interface StylePreference {
  id?: string;
  pref_id?: string;
  category: string;
  key: string;
  value: unknown;
  confidence?: number;
  status?: "applied" | "rejected" | "deleted" | string;
  provenance?: {
    source?: string; // approval | correction | promotion
    verified?: boolean;
    run_id?: string | null;
    stage?: string | null;
    decision_ref?: string | null;
    from_pref?: string | null;
    note?: string | null;
  };
}

export interface PreferenceScopeBlock {
  opted_out: boolean;
  preferences: StylePreference[];
}

export interface PreferencesPayload {
  categories: string[];
  global?: PreferenceScopeBlock;
  project?: PreferenceScopeBlock;
}

export function prefId(p: StylePreference): string {
  return String(p.pref_id ?? p.id ?? "");
}
