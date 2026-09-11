from __future__ import annotations

import ipaddress
import os
from datetime import date
from typing import Optional

import psycopg
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from psycopg.rows import dict_row
from werkzeug.exceptions import HTTPException


def create_app(config: Optional[dict] = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE_URL=os.getenv("DATABASE_URL", "postgresql://iptracker:iptracker@localhost/iptracker"),
        PAGE_SIZE=100,
    )
    if config:
        app.config.update(config)

    def db():
        if "db" not in g:
            g.db = psycopg.connect(app.config["DATABASE_URL"], row_factory=dict_row)
        return g.db

    @app.teardown_appcontext
    def close_db(_error=None):
        connection = g.pop("db", None)
        if connection:
            connection.close()

    @app.errorhandler(HTTPException)
    def api_http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error={"code": error.code, "message": error.description}), error.code
        return error

    def snapshots():
        return db().execute(
            "SELECT snapshot_date FROM snapshots ORDER BY snapshot_date DESC"
        ).fetchall()

    def selected_snapshot(value: str | None):
        if value:
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                abort(400, "Snapshot must be in YYYY-MM-DD format")
            row = db().execute(
                "SELECT snapshot_date FROM snapshots WHERE snapshot_date=%s", (parsed,)
            ).fetchone()
        else:
            row = db().execute(
                "SELECT snapshot_date FROM snapshots ORDER BY snapshot_date DESC LIMIT 1"
            ).fetchone()
        if not row:
            abort(404, "No matching imported snapshot")
        return row

    @app.context_processor
    def common_context():
        return {"all_snapshots": snapshots()}

    @app.get("/")
    def index():
        latest = db().execute(
            "SELECT s.snapshot_date,s.imported_at,"
            "(SELECT count(*) FROM route_versions r WHERE r.valid_from<=s.snapshot_date "
            " AND (r.valid_until IS NULL OR r.valid_until>s.snapshot_date)) AS announcements,"
            "(SELECT count(*) FROM asn_versions d WHERE d.valid_from<=s.snapshot_date "
            " AND (d.valid_until IS NULL OR d.valid_until>s.snapshot_date)) AS asns "
            "FROM snapshots s ORDER BY snapshot_date DESC"
        ).fetchall()
        return render_template("index.html", snapshots=latest)

    @app.get("/ip")
    def ip_lookup():
        value = request.args.get("address", "").strip()
        if not value:
            return render_template("ip.html", address="", routes=None)
        try:
            address = str(ipaddress.ip_address(value))
        except ValueError:
            return render_template("ip.html", address=value, routes=[], error="Enter a valid IPv4 or IPv6 address"), 400
        snapshot = selected_snapshot(request.args.get("snapshot"))
        routes = db().execute(
            "SELECT a.prefix,a.asn,d.name,d.class,d.country_code FROM route_versions a "
            "LEFT JOIN asn_versions d ON d.asn=a.asn AND d.valid_from<=%s "
            " AND (d.valid_until IS NULL OR d.valid_until>%s) "
            "WHERE a.valid_from<=%s AND (a.valid_until IS NULL OR a.valid_until>%s) "
            " AND a.prefix >>= %s::inet ORDER BY masklen(a.prefix) DESC,a.asn",
            (snapshot["snapshot_date"],) * 4 + (address,),
        ).fetchall()
        return render_template("ip.html", address=address, routes=routes, selected=snapshot)

    @app.get("/asns")
    def asn_list():
        snapshot = selected_snapshot(request.args.get("snapshot"))
        query = request.args.get("q", "").strip()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        params = [snapshot["snapshot_date"], snapshot["snapshot_date"],
                  snapshot["snapshot_date"], snapshot["snapshot_date"]]
        where = ""
        if query:
            asn_query = query.upper().removeprefix("AS")
            where = " AND (d.name ILIKE %s OR d.country_code ILIKE %s"
            params.extend([f"%{query}%", query])
            if asn_query.isdigit():
                where += " OR d.asn=%s"
                params.append(int(asn_query))
            where += ")"
        params.extend([app.config["PAGE_SIZE"] + 1, (page - 1) * app.config["PAGE_SIZE"]])
        rows = db().execute(
            "SELECT d.asn,d.name,d.class,d.country_code,count(a.prefix) AS prefix_count "
            "FROM asn_versions d LEFT JOIN route_versions a ON a.asn=d.asn "
            " AND a.valid_from<=%s AND (a.valid_until IS NULL OR a.valid_until>%s) "
            "WHERE d.valid_from<=%s AND (d.valid_until IS NULL OR d.valid_until>%s)" + where +
            " GROUP BY d.asn,d.name,d.class,d.country_code "
            "ORDER BY d.asn LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        has_next = len(rows) > app.config["PAGE_SIZE"]
        return render_template("asns.html", rows=rows[:app.config["PAGE_SIZE"]], selected=snapshot,
                               q=query, page=page, has_next=has_next)

    @app.get("/asn/<int:asn>")
    def asn_detail(asn: int):
        snapshot = selected_snapshot(request.args.get("snapshot"))
        try:
            changes_page = max(1, int(request.args.get("changes_page", 1)))
        except ValueError:
            changes_page = 1
        detail = db().execute(
            "SELECT * FROM asn_versions WHERE asn=%s AND valid_from<=%s "
            "AND (valid_until IS NULL OR valid_until>%s)",
            (asn, snapshot["snapshot_date"], snapshot["snapshot_date"]),
        ).fetchone()
        if not detail:
            abort(404)
        prefixes = db().execute(
            "SELECT prefix FROM route_versions WHERE asn=%s AND valid_from<=%s "
            "AND (valid_until IS NULL OR valid_until>%s) "
            "ORDER BY family(prefix),prefix LIMIT 1000",
            (asn, snapshot["snapshot_date"], snapshot["snapshot_date"]),
        ).fetchall()
        history = db().execute(
            "SELECT s.snapshot_date,d.name,d.class,d.country_code,"
            "(SELECT count(*) FROM route_versions r WHERE r.asn=%s AND r.valid_from<=s.snapshot_date "
            " AND (r.valid_until IS NULL OR r.valid_until>s.snapshot_date)) AS prefix_count "
            "FROM snapshots s JOIN asn_versions d ON d.asn=%s AND d.valid_from<=s.snapshot_date "
            " AND (d.valid_until IS NULL OR d.valid_until>s.snapshot_date) "
            "ORDER BY s.snapshot_date DESC", (asn, asn),
        ).fetchall()
        change_page_size = 50
        changes = db().execute(
            "SELECT snapshot_date,prefix,old_asns,new_asns,CASE "
            "WHEN %s::bigint=ANY(new_asns) AND NOT (%s::bigint=ANY(old_asns)) THEN 'acquired' "
            "WHEN %s::bigint=ANY(old_asns) AND NOT (%s::bigint=ANY(new_asns)) THEN 'released' "
            "ELSE 'origin set changed' END AS action "
            "FROM prefix_changes WHERE %s::bigint=ANY(old_asns) OR %s::bigint=ANY(new_asns) "
            "ORDER BY snapshot_date DESC,prefix LIMIT %s OFFSET %s",
            (asn, asn, asn, asn, asn, asn, change_page_size + 1,
             (changes_page - 1) * change_page_size),
        ).fetchall()
        changes_has_next = len(changes) > change_page_size
        return render_template(
            "asn.html", detail=detail, prefixes=prefixes, history=history, selected=snapshot,
            changes=changes[:change_page_size], changes_page=changes_page,
            changes_has_next=changes_has_next,
        )

    @app.get("/block")
    def block_detail():
        value = request.args.get("prefix", "").strip()
        try:
            prefix = str(ipaddress.ip_network(value, strict=False))
        except ValueError:
            abort(400, "Enter a valid network prefix")
        history = db().execute(
            "SELECT r.valid_from,r.valid_until,r.asn,d.name FROM route_versions r "
            "LEFT JOIN asn_versions d ON d.asn=r.asn AND d.valid_from<=r.valid_from "
            " AND (d.valid_until IS NULL OR d.valid_until>r.valid_from) "
            "WHERE r.prefix=%s::cidr ORDER BY r.valid_from DESC,r.asn",
            (prefix,),
        ).fetchall()
        return render_template("block.html", prefix=prefix, history=history)

    @app.get("/history")
    def history():
        filters = {
            "date_from": request.args.get("date_from", "").strip(),
            "date_to": request.args.get("date_to", "").strip(),
            "prefix": request.args.get("prefix", "").strip(),
            "asn": request.args.get("asn", "").strip(),
            "kind": request.args.get("kind", "").strip(),
        }
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        where = []
        params = []
        for key, operator in (("date_from", ">="), ("date_to", "<=")):
            if filters[key]:
                try:
                    parsed_date = date.fromisoformat(filters[key])
                except ValueError:
                    abort(400, f"{key.replace('_', ' ').title()} must be YYYY-MM-DD")
                where.append(f"snapshot_date {operator} %s")
                params.append(parsed_date)
        if filters["prefix"]:
            try:
                parsed_prefix = str(ipaddress.ip_network(filters["prefix"], strict=False))
            except ValueError:
                abort(400, "Prefix must be a valid IPv4 or IPv6 network")
            filters["prefix"] = parsed_prefix
            where.append("prefix=%s::cidr")
            params.append(parsed_prefix)
        if filters["asn"]:
            asn_value = filters["asn"].upper().removeprefix("AS")
            if not asn_value.isdigit():
                abort(400, "ASN must be a number or use the AS123 form")
            filters["asn"] = asn_value
            where.append("(%s::bigint=ANY(old_asns) OR %s::bigint=ANY(new_asns))")
            params.extend([int(asn_value), int(asn_value)])
        kind_sql = {
            "added": "cardinality(old_asns)=0",
            "withdrawn": "cardinality(new_asns)=0",
            "changed": "cardinality(old_asns)>0 AND cardinality(new_asns)>0",
        }
        if filters["kind"]:
            if filters["kind"] not in kind_sql:
                abort(400, "Unknown change type")
            where.append(kind_sql[filters["kind"]])
        clause = " WHERE " + " AND ".join(where) if where else ""
        page_size = app.config["PAGE_SIZE"]
        params.extend([page_size + 1, (page - 1) * page_size])
        rows = db().execute(
            "SELECT prefix,old_asns,new_asns,snapshot_date FROM prefix_changes "
            + clause + " ORDER BY snapshot_date DESC,prefix LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        has_next = len(rows) > page_size
        return render_template("history.html", rows=rows[:page_size], filters=filters,
                               page=page, has_next=has_next)

    @app.post("/search")
    def search():
        value = request.form.get("query", "").strip()
        if not value:
            return redirect(url_for("index"))
        try:
            ipaddress.ip_address(value)
            return redirect(url_for("ip_lookup", address=value))
        except ValueError:
            pass
        if value.upper().removeprefix("AS").isdigit():
            return redirect(url_for("asn_detail", asn=int(value.upper().removeprefix("AS"))))
        return redirect(url_for("asn_list", q=value))

    def api_page():
        try:
            page = max(1, int(request.args.get("page", 1)))
            per_page = min(500, max(1, int(request.args.get("per_page", app.config["PAGE_SIZE"]))))
        except ValueError:
            abort(400, "page and per_page must be integers")
        return page, per_page

    def page_response(items, page, per_page):
        has_next = len(items) > per_page
        return jsonify({
            "data": items[:per_page],
            "pagination": {"page": page, "per_page": per_page,
                           "has_previous": page > 1, "has_next": has_next},
        })

    @app.get("/api/v1/snapshots")
    def api_snapshots():
        rows = db().execute(
            "SELECT snapshot_date,blocks_filename,asns_filename,imported_at "
            "FROM snapshots ORDER BY snapshot_date DESC"
        ).fetchall()
        return jsonify(data=[{
            "date": row["snapshot_date"].isoformat(),
            "blocks_filename": row["blocks_filename"],
            "asns_filename": row["asns_filename"],
            "imported_at": row["imported_at"].isoformat(),
        } for row in rows])

    @app.get("/api/v1/ip/<path:address>")
    def api_ip(address: str):
        try:
            normalized = str(ipaddress.ip_address(address))
        except ValueError:
            abort(400, "invalid IPv4 or IPv6 address")
        snapshot = selected_snapshot(request.args.get("snapshot"))
        rows = db().execute(
            "SELECT a.prefix,a.asn,d.name,d.class,d.country_code FROM route_versions a "
            "LEFT JOIN asn_versions d ON d.asn=a.asn AND d.valid_from<=%s "
            " AND (d.valid_until IS NULL OR d.valid_until>%s) "
            "WHERE a.valid_from<=%s AND (a.valid_until IS NULL OR a.valid_until>%s) "
            " AND a.prefix >>= %s::inet ORDER BY masklen(a.prefix) DESC,a.asn",
            (snapshot["snapshot_date"],) * 4 + (normalized,),
        ).fetchall()
        return jsonify(data={
            "address": normalized,
            "snapshot": snapshot["snapshot_date"].isoformat(),
            "routes": [{"prefix": str(row["prefix"]), "asn": row["asn"],
                        "name": row["name"], "class": row["class"],
                        "country_code": row["country_code"]} for row in rows],
        })

    @app.get("/api/v1/asns")
    def api_asns():
        snapshot = selected_snapshot(request.args.get("snapshot"))
        page, per_page = api_page()
        query = request.args.get("q", "").strip()
        params = [snapshot["snapshot_date"], snapshot["snapshot_date"],
                  snapshot["snapshot_date"], snapshot["snapshot_date"]]
        where = ""
        if query:
            numeric = query.upper().removeprefix("AS")
            where = " AND (d.name ILIKE %s OR d.country_code ILIKE %s"
            params.extend([f"%{query}%", query])
            if numeric.isdigit():
                where += " OR d.asn=%s"
                params.append(int(numeric))
            where += ")"
        params.extend([per_page + 1, (page - 1) * per_page])
        rows = db().execute(
            "SELECT d.asn,d.name,d.class,d.country_code,count(r.prefix) prefix_count "
            "FROM asn_versions d LEFT JOIN route_versions r ON r.asn=d.asn "
            " AND r.valid_from<=%s AND (r.valid_until IS NULL OR r.valid_until>%s) "
            "WHERE d.valid_from<=%s AND (d.valid_until IS NULL OR d.valid_until>%s)" + where +
            " GROUP BY d.asn,d.name,d.class,d.country_code ORDER BY d.asn LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        items = [{"asn": r["asn"], "name": r["name"], "class": r["class"],
                  "country_code": r["country_code"], "prefix_count": r["prefix_count"],
                  "snapshot": snapshot["snapshot_date"].isoformat()} for r in rows]
        return page_response(items, page, per_page)

    @app.get("/api/v1/asns/<int:asn>")
    def api_asn(asn: int):
        snapshot = selected_snapshot(request.args.get("snapshot"))
        page, per_page = api_page()
        detail = db().execute(
            "SELECT asn,name,class,country_code FROM asn_versions WHERE asn=%s "
            "AND valid_from<=%s AND (valid_until IS NULL OR valid_until>%s)",
            (asn, snapshot["snapshot_date"], snapshot["snapshot_date"]),
        ).fetchone()
        if not detail:
            abort(404, f"AS{asn} does not exist in this snapshot")
        prefixes = db().execute(
            "SELECT prefix FROM route_versions WHERE asn=%s AND valid_from<=%s "
            "AND (valid_until IS NULL OR valid_until>%s) ORDER BY family(prefix),prefix "
            "LIMIT %s OFFSET %s",
            (asn, snapshot["snapshot_date"], snapshot["snapshot_date"],
             per_page + 1, (page - 1) * per_page),
        ).fetchall()
        has_next = len(prefixes) > per_page
        return jsonify(data={"asn": detail["asn"], "name": detail["name"],
                             "class": detail["class"], "country_code": detail["country_code"],
                             "snapshot": snapshot["snapshot_date"].isoformat(),
                             "prefixes": [str(row["prefix"]) for row in prefixes[:per_page]]},
                       pagination={"page": page, "per_page": per_page,
                                   "has_previous": page > 1, "has_next": has_next})

    @app.get("/api/v1/blocks/<path:prefix>/history")
    def api_block_history(prefix: str):
        try:
            normalized = str(ipaddress.ip_network(prefix, strict=False))
        except ValueError:
            abort(400, "invalid IPv4 or IPv6 prefix")
        rows = db().execute(
            "SELECT valid_from,valid_until,asn FROM route_versions "
            "WHERE prefix=%s::cidr ORDER BY valid_from DESC,asn", (normalized,)
        ).fetchall()
        return jsonify(data={"prefix": normalized, "versions": [{
            "asn": row["asn"], "valid_from": row["valid_from"].isoformat(),
            "valid_until": row["valid_until"].isoformat() if row["valid_until"] else None,
        } for row in rows]})

    @app.get("/api/v1/changes")
    def api_changes():
        page, per_page = api_page()
        where, params = [], []
        for key, operator in (("date_from", ">="), ("date_to", "<=")):
            value = request.args.get(key, "").strip()
            if value:
                try:
                    params.append(date.fromisoformat(value))
                except ValueError:
                    abort(400, f"{key} must be YYYY-MM-DD")
                where.append(f"snapshot_date {operator} %s")
        prefix = request.args.get("prefix", "").strip()
        if prefix:
            try:
                prefix = str(ipaddress.ip_network(prefix, strict=False))
            except ValueError:
                abort(400, "invalid IPv4 or IPv6 prefix")
            where.append("prefix=%s::cidr")
            params.append(prefix)
        asn = request.args.get("asn", "").strip().upper().removeprefix("AS")
        if asn:
            if not asn.isdigit():
                abort(400, "asn must be a number or use the AS123 form")
            where.append("(%s::bigint=ANY(old_asns) OR %s::bigint=ANY(new_asns))")
            params.extend([int(asn), int(asn)])
        kind = request.args.get("kind", "").strip()
        kinds = {"added": "cardinality(old_asns)=0", "withdrawn": "cardinality(new_asns)=0",
                 "changed": "cardinality(old_asns)>0 AND cardinality(new_asns)>0"}
        if kind:
            if kind not in kinds:
                abort(400, "kind must be added, withdrawn, or changed")
            where.append(kinds[kind])
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.extend([per_page + 1, (page - 1) * per_page])
        rows = db().execute(
            "SELECT snapshot_date,prefix,old_asns,new_asns FROM prefix_changes" + clause +
            " ORDER BY snapshot_date DESC,prefix LIMIT %s OFFSET %s", params
        ).fetchall()
        items = [{"date": r["snapshot_date"].isoformat(), "prefix": str(r["prefix"]),
                  "old_asns": r["old_asns"], "new_asns": r["new_asns"]} for r in rows]
        return page_response(items, page, per_page)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=os.getenv("FLASK_DEBUG") == "1")
