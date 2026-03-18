import os
import json
import datetime as dt

import pandas as pd

from yieldbook_api import login, get_hist_data, post_sync, run_bond_py, get_actual_vs_projected


def _month_start(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def _add_months(d: dt.date, months: int) -> dt.date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return dt.date(y, m, 1)


def _last_12_month_window(today: dt.date) -> tuple[dt.date, dt.date]:
    """
    Returns [start, end] as month-start dates spanning the last 12 months including current month.
    Example: if today is 2026-03-17, start=2025-04-01, end=2026-03-01
    """
    end = _month_start(today)
    start = _add_months(end, -11)
    return start, end


def _month_end(d: dt.date) -> dt.date:
    nxt = _add_months(_month_start(d), 1)
    return nxt - dt.timedelta(days=1)


def _read_cusips_from_excel(path: str) -> list[str]:
    # Your workbook has no headers (row 1 is a label). We treat col 0 as CUSIP list.
    df = pd.read_excel(path, sheet_name=0, header=None)
    raw = (
        df.iloc[1:, 0]
        .astype(str)
        .str.strip()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
        .dropna()
        .tolist()
    )
    # keep order, de-dupe
    seen = set()
    cusips = []
    for c in raw:
        c = c.upper()
        if c not in seen:
            seen.add(c)
            cusips.append(c)
    return cusips


def _extract_lt_cpr_from_py_result(item: dict) -> float | None:
    py = item.get("py") if isinstance(item, dict) else None
    if not isinstance(py, dict):
        return None
    ppm = py.get("dataPpmProjList") or []
    if not isinstance(ppm, list):
        return None
    # Prefer explicit CPR projection
    for p in ppm:
        if isinstance(p, dict) and (p.get("prepayType") == "CPR" or p.get("type") == "CPR"):
            v = p.get("longTerm")
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
    # Fallback: mirror tba_analysis.py assumption that [1] is CPR
    if len(ppm) > 1 and isinstance(ppm[1], dict):
        v = ppm[1].get("longTerm")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _extract_actual_cpr_series(avp_resp: dict) -> list[dict]:
    """
    Yieldbook actual-vs-projected response -> list of points with {date, actualCPR}.
    """
    if not isinstance(avp_resp, dict):
        return []
    data = avp_resp.get("data") if isinstance(avp_resp.get("data"), dict) else avp_resp
    act = data.get("actVsProjPrepay") if isinstance(data, dict) else None
    if not isinstance(act, dict):
        return []
    vec = act.get("dataActVsProjVectorList")
    if not isinstance(vec, list):
        return []
    out = []
    for p in vec:
        if not isinstance(p, dict):
            continue
        d = p.get("date")
        if not d:
            continue
        out.append({"date": str(d)[:10], "actualCPR": p.get("actualCPR")})
    return out


def _normalize_hist_response_to_table(resp: dict, field: str) -> pd.DataFrame:
    """
    Best-effort normalizer: turns common Yieldbook hist-data response shapes into a table:
    rows=CUSIP, cols=YYYY-MM, values=field
    """
    # New preferred shape in this workspace: {cusip: response, ...}
    if isinstance(resp, dict) and resp and all(isinstance(v, dict) for v in resp.values()):
        frames = []
        for cusip, r in resp.items():
            try:
                df = _normalize_hist_response_to_table(r, field)
                if df.index.name != "CUSIP":
                    df.index.name = "CUSIP"
                # Ensure we keep the original 9-char CUSIP key as row label
                if len(df.index) == 1:
                    df.index = [cusip]
                frames.append(df)
            except Exception:
                continue
        if frames:
            return pd.concat(frames, axis=0)

    # Common candidates for where the series live.
    candidates = [
        resp.get("data"),
        resp.get("results"),
        resp.get("result"),
        resp.get("securities"),
        resp.get("bonds"),
    ]
    items = next((c for c in candidates if isinstance(c, list)), None)
    if items is None and isinstance(resp, list):
        items = resp
    if items is None:
        # Some responses return only meta+errors (no data payload)
        if isinstance(resp, dict) and (resp.get("errors") or resp.get("meta")):
            return pd.DataFrame()
        raise ValueError(
            "Unrecognized hist-data response shape. "
            "Set YIELDBOOK_CPR_DEBUG=1 to write raw response."
        )

    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cusip = (it.get("id") or it.get("cusip") or it.get("CUSIP") or "").strip()
        # Find the time series container
        series = (
            it.get("series")
            or it.get("histData")
            or it.get("history")
            or it.get("data")
            or it.get(field)
        )
        # Normalize series into list of {date/value}
        points = []
        if isinstance(series, list):
            points = series
        elif isinstance(series, dict):
            # could be {field: [...]} or {date: value}
            if field in series and isinstance(series[field], list):
                points = series[field]
            else:
                # treat as mapping date->value
                points = [{"date": k, "value": v} for k, v in series.items()]
        else:
            points = []

        for p in points:
            if not isinstance(p, dict):
                continue
            d = p.get("date") or p.get("asOfDate") or p.get("month") or p.get("period")
            v = (
                p.get("value")
                if "value" in p
                else p.get(field) or p.get(field.lower()) or p.get("cpr") or p.get("CPR")
            )
            if not d:
                continue
            d_str = str(d)[:10]
            # we want YYYY-MM columns
            ym = d_str[:7]
            rows.append({"CUSIP": cusip, "YYYY-MM": ym, field: v})

    if not rows:
        raise ValueError(f"No points found for field {field}.")

    df = pd.DataFrame(rows)
    df = df.pivot_table(index="CUSIP", columns="YYYY-MM", values=field, aggfunc="last")
    df = df.sort_index()
    df.columns.name = None
    return df


def main():
    input_xlsx = os.getenv("YIELDBOOK_CPR_INPUT_XLSX", "tba_analysis_input.xlsx")
    output_xlsx = os.getenv("YIELDBOOK_CPR_OUTPUT_XLSX", "tba_analysis_input_with_cpr.xlsx")
    out_sheet = os.getenv("YIELDBOOK_CPR_OUTPUT_SHEET", "cpr_hist_12m")

    endpoint = os.getenv("YIELDBOOK_CPR_ENDPOINT", "py-lt-cpr").strip().lower()
    # Yieldbook hist-data uses `keyword=...` (can be comma-separated).
    # Keep old env name for compatibility; prefer YIELDBOOK_CPR_KEYWORD going forward.
    cpr_keyword = (
        os.getenv("YIELDBOOK_CPR_KEYWORD")
        or os.getenv("YIELDBOOK_CPR_FIELD")
        or "CPR"
    ).strip()
    # Your sample uses frequency=DAILY; for last-12-month monthly history use MONTHLY by default.
    frequency = os.getenv("YIELDBOOK_CPR_FREQUENCY", "MONTHLY").strip()

    today = dt.date.today()
    start, end = _last_12_month_window(today)

    cusips = _read_cusips_from_excel(input_xlsx)
    if not cusips:
        raise SystemExit(f"No CUSIPs found in first column of {input_xlsx}")

    token = login()

    if endpoint in {"py", "py-lt-cpr", "lt-cpr"}:
        # Build month-end dates for last 12 months and run /sync/bond/py for each month.
        # This produces a "historical" series of model-implied LongTerm fwd CPR.
        py_level = float(os.getenv("YIELDBOOK_PY_LEVEL", "100"))
        curve_type = os.getenv("YIELDBOOK_CURVE_TYPE", "SWAP_RFR")
        prepay_type = os.getenv("YIELDBOOK_PREPAY_TYPE", "Model")
        prepay_rate = int(float(os.getenv("YIELDBOOK_PREPAY_RATE", "100")))
        vol_type = os.getenv("YIELDBOOK_VOL_TYPE", "Default")

        months = []
        cur = start
        for _ in range(12):
            months.append(_month_end(cur))
            cur = _add_months(cur, 1)

        # Table: rows=cusip, cols=YYYY-MM (month end), values=lt_cpr
        out = pd.DataFrame(index=cusips, columns=[m.strftime("%Y-%m") for m in months], dtype="float64")
        out.index.name = "CUSIP"
        errors_rows = []

        # Speed: batch all months in one request per CUSIP by running months sequentially,
        # but add a timeout per month and keep going.
        request_timeout_s = int(float(os.getenv("YIELDBOOK_PY_TIMEOUT_S", "120")))
        max_months = int(float(os.getenv("YIELDBOOK_PY_MAX_MONTHS", "12")))

        for i, m in enumerate(months[:max_months]):
            pricing_date = m.isoformat()
            inputs = []
            for c in cusips:
                inputs.append(
                    {
                        "identifier": c,
                        "idType": "securityIDEntry",
                        "userTag": c,
                        "level": f"{py_level}",
                        "curve": {"curveType": curve_type},
                        "prepaySettings": {"type": prepay_type, "rate": prepay_rate},
                        "volatility": {"type": vol_type},
                        "extraSettings": {"optionModel": "OASEDUR"},
                    }
                )
            try:
                resp = run_bond_py(
                    token=token,
                    pricing_date=pricing_date,
                    inputs=inputs,
                )
            except Exception as e:
                errors_rows.append(
                    {
                        "CUSIP": "",
                        "code": "PY_REQUEST_FAILED",
                        "description": f"pricingDate={pricing_date} err={type(e).__name__}: {str(e)[:400]}",
                        "resolution": "",
                    }
                )
                continue
            results = resp.get("results") if isinstance(resp, dict) else None
            if not isinstance(results, list):
                errors_rows.append(
                    {
                        "CUSIP": "",
                        "code": "PY_NO_RESULTS",
                        "description": f"pricingDate={pricing_date}",
                        "resolution": "Check response",
                    }
                )
                continue

            # Map result -> cusip
            for item in results:
                py = item.get("py") if isinstance(item, dict) else None
                cusip = None
                if isinstance(py, dict):
                    cusip = (py.get("cusip") or "").strip()
                cusip = cusip or (item.get("userTag") if isinstance(item, dict) else None)
                if not cusip:
                    continue
                v = _extract_lt_cpr_from_py_result(item)
                if v is not None and cusip in out.index:
                    out.loc[cusip, m.strftime("%Y-%m")] = v
                else:
                    diag = (py.get("diagnostic") if isinstance(py, dict) else None) if py else None
                    if diag:
                        errors_rows.append(
                            {"CUSIP": cusip, "code": "PY_DIAGNOSTIC", "description": str(diag)[:500], "resolution": ""}
                        )

        # Write workbook (if the file is open in Excel, fall back to a timestamped filename)
        try_paths = [output_xlsx]
        if output_xlsx.lower().endswith(".xlsx"):
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            try_paths.append(output_xlsx[:-5] + f"_{stamp}.xlsx")

        written_to = None
        last_err = None
        for p in try_paths:
            try:
                with pd.ExcelWriter(p, engine="openpyxl", mode="w") as writer:
                    original = pd.read_excel(input_xlsx, sheet_name=0, header=None)
                    original.to_excel(
                        writer,
                        sheet_name="tba_analysis_results",
                        index=False,
                        header=False,
                    )
                    out.reset_index().to_excel(writer, sheet_name=out_sheet, index=False)
                    if errors_rows:
                        pd.DataFrame(errors_rows).to_excel(
                            writer, sheet_name=f"{out_sheet}_errors", index=False
                        )
                written_to = p
                break
            except PermissionError as e:
                last_err = e
                continue
        if not written_to:
            raise last_err or PermissionError(f"Could not write output to {try_paths}")

        print(f"Wrote {out_sheet} to {written_to}")
        return

    if endpoint in {"actual-vs-projected", "avp", "actualcpr"}:
        # Pull ActualCPR time series from /sync/bond/actual-vs-projected/{id}
        # Then take the last 12 months (month-end) values into a YYYY-MM grid.
        months = []
        cur = start
        for _ in range(12):
            months.append(f"{cur.year:04d}-{cur.month:02d}")
            cur = _add_months(cur, 1)

        table = pd.DataFrame(index=cusips, columns=months, dtype="float64")
        table.index.name = "CUSIP"
        errors_rows = []

        for c in cusips:
            try:
                resp = get_actual_vs_projected(token, c)
                pts = _extract_actual_cpr_series(resp)
                if not pts:
                    errors_rows.append(
                        {"CUSIP": c, "code": "AVP_NO_POINTS", "description": "No vectors returned", "resolution": ""}
                    )
                    continue
                df = pd.DataFrame(pts)
                df["YYYY-MM"] = df["date"].str.slice(0, 7)
                # if duplicates per month, take last
                m = (
                    df.dropna(subset=["actualCPR"])
                    .groupby("YYYY-MM")["actualCPR"]
                    .last()
                )
                for mm, v in m.items():
                    if mm in table.columns:
                        try:
                            table.loc[c, mm] = float(v)
                        except (TypeError, ValueError):
                            pass
            except Exception as e:
                errors_rows.append(
                    {
                        "CUSIP": c,
                        "code": "AVP_REQUEST_FAILED",
                        "description": f"{type(e).__name__}: {str(e)[:400]}",
                        "resolution": "",
                    }
                )

        # Write to workbook (handles locked file)
        try_paths = [output_xlsx]
        if output_xlsx.lower().endswith(".xlsx"):
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            try_paths.append(output_xlsx[:-5] + f"_{stamp}.xlsx")

        written_to = None
        last_err = None
        for p in try_paths:
            try:
                with pd.ExcelWriter(p, engine="openpyxl", mode="w") as writer:
                    original = pd.read_excel(input_xlsx, sheet_name=0, header=None)
                    original.to_excel(writer, sheet_name="tba_analysis_results", index=False, header=False)
                    table.reset_index().to_excel(writer, sheet_name=out_sheet, index=False)
                    if errors_rows:
                        pd.DataFrame(errors_rows).to_excel(
                            writer, sheet_name=f"{out_sheet}_errors", index=False
                        )
                written_to = p
                break
            except PermissionError as e:
                last_err = e
                continue
        if not written_to:
            raise last_err or PermissionError(f"Could not write output to {try_paths}")

        print(f"Wrote {out_sheet} to {written_to}")
        return

    if endpoint == "hist-data":
        resp = get_hist_data(
            token=token,
            cusips=cusips,
            fields=[cpr_keyword],
            start_date=start,
            end_date=end,
            frequency=frequency,
        )
    elif endpoint in {"cash-flow", "cashflow"}:
        # Some tenants expose CPR/prepay metrics via cash-flow. This call is left flexible:
        # set YIELDBOOK_CPR_PATH and any extra JSON via YIELDBOOK_CPR_EXTRA_JSON.
        path = os.getenv("YIELDBOOK_CPR_PATH", "/sync/bond/cash-flow")
        extra = os.getenv("YIELDBOOK_CPR_EXTRA_JSON", "").strip()
        extra_payload = json.loads(extra) if extra else {}
        payload = {"idType": "CUSIP", "ids": cusips, **extra_payload}
        resp = post_sync(token, path, payload)
    elif endpoint in {"collateral-details", "collateral"}:
        path = os.getenv("YIELDBOOK_CPR_PATH", "/sync/bond/collateral-details")
        extra = os.getenv("YIELDBOOK_CPR_EXTRA_JSON", "").strip()
        extra_payload = json.loads(extra) if extra else {}
        payload = {"idType": "CUSIP", "ids": cusips, **extra_payload}
        resp = post_sync(token, path, payload)
    else:
        raise SystemExit(
            "Unknown YIELDBOOK_CPR_ENDPOINT. Use hist-data, cash-flow, or collateral-details."
        )

    if os.getenv("YIELDBOOK_CPR_DEBUG", "0") == "1":
        with open("yieldbook_cpr_raw_response.json", "w", encoding="utf-8") as f:
            json.dump(resp, f, indent=2)

    # Capture per-CUSIP errors if the endpoint returns meta+errors only
    errors_rows = []
    if isinstance(resp, dict) and resp and all(isinstance(v, dict) for v in resp.values()):
        for c, r in resp.items():
            errs = r.get("errors") if isinstance(r, dict) else None
            if errs:
                for e in errs:
                    if isinstance(e, dict):
                        errors_rows.append(
                            {
                                "CUSIP": c,
                                "code": e.get("code"),
                                "description": e.get("description"),
                                "resolution": e.get("resolution"),
                            }
                        )

    # Normalization expects a single field label for the value column; we use the keyword.
    table = _normalize_hist_response_to_table(resp, cpr_keyword)

    # Ensure columns cover exactly the 12 months window (fill missing with NaN)
    months = []
    cur = start
    for _ in range(12):
        months.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = _add_months(cur, 1)
    if table.empty:
        table = pd.DataFrame(index=cusips, columns=months, dtype="float64")
        table.index.name = "CUSIP"
    else:
        table = table.reindex(columns=months)

    # Write to new workbook while preserving original sheets
    with pd.ExcelWriter(output_xlsx, engine="openpyxl", mode="w") as writer:
        # Copy original first sheet content
        original = pd.read_excel(input_xlsx, sheet_name=0, header=None)
        original.to_excel(writer, sheet_name="tba_analysis_results", index=False, header=False)
        table.reset_index().to_excel(writer, sheet_name=out_sheet, index=False)
        if errors_rows:
            pd.DataFrame(errors_rows).to_excel(
                writer, sheet_name=f"{out_sheet}_errors", index=False
            )

    print(f"Wrote {out_sheet} to {output_xlsx}")


if __name__ == "__main__":
    main()

