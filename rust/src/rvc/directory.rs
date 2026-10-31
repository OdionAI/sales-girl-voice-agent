//! Fresh, scoped provider listings. Reads never authorize an email; email sends
//! consume a prepared destination and a durable, unique outbox reservation.
use super::{
    actions::{ActionResult, Decision, Pending, Permit},
    identity::VerifiedSession,
    profiles::AgentContext,
    providers::Providers,
};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, OnceLock},
    time::Duration,
};
use unicode_normalization::UnicodeNormalization;
mod email;
pub use email::speech_text;
pub use email::{Contact, Prepare, Prepared};

pub const LOOKUP: &str = "lookup_provider_directory";
pub const EMAIL: &str = "email_provider_results";
pub const LOCATION: &str = "directory_location_confirmation";
pub fn is_tool(name: &str) -> bool {
    matches!(name, LOOKUP | EMAIL)
}
pub fn greeting(original: &str, name: &str, language: &str) -> String {
    let name = name
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(120)
        .collect::<String>();
    if name.is_empty() || super::languages::uses_sbpn(language) {
        return original.into();
    }
    if original.contains("{caller_name}") {
        return original.replace("{caller_name}", &name);
    }
    if original.to_lowercase().contains(&name.to_lowercase()) {
        return original.into();
    }
    regex::Regex::new(r"(?i)^(Hello|Hi|Good morning|Good afternoon|Good evening)(?:\s+(?:there|everyone))?\s*[,!:.]?\s*").map(|r|r.replace(original,|c:&regex::Captures<'_>|format!("{} {name}, ",&c[1])).into_owned()).unwrap_or_else(|_|original.into())
}
pub fn data() -> &'static Value {
    static DATA: OnceLock<Value> = OnceLock::new();
    DATA.get_or_init(|| {
        serde_json::from_str(include_str!("directory_data.json")).unwrap_or(Value::Null)
    })
}
pub fn enabled(ctx: &AgentContext, name: &str) -> bool {
    let url = match name {
        LOOKUP => "builtin://provider-directory",
        EMAIL => "builtin://email-provider-results",
        _ => return false,
    };
    ctx.tools
        .iter()
        .any(|t| t["name"] == name && t["url"] == url && t["is_active"] != false)
        && (name != EMAIL || enabled(ctx, LOOKUP))
}
pub fn definitions(ctx: &AgentContext) -> Vec<Value> {
    data()["definitions"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|d| enabled(ctx, string(&d["function"]["name"])))
        .cloned()
        .collect()
}
fn string(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
fn result(status: &str, message: &str, details: Value) -> Value {
    json!({"status":status,"message":message,"data":details})
}
pub fn norm(v: &Value) -> String {
    let owned;
    let s = if let Some(s) = v.as_str() {
        s
    } else if v.is_null() || v == false || v == 0 {
        ""
    } else {
        owned = v.to_string();
        &owned
    };
    s.nfkd()
        .filter(char::is_ascii)
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                ' '
            }
        })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
fn n(s: &str) -> String {
    norm(&json!(s))
}
fn contains(query: &str, value: &str) -> bool {
    let q = n(query);
    let v = n(value);
    q.split_whitespace()
        .all(|w| v.split_whitespace().any(|p| p == w))
}
fn locality(query: &str, values: &[&str]) -> bool {
    n(query).is_empty() || values.iter().any(|v| !v.is_empty() && contains(query, v))
}
fn town(v: &Value) -> String {
    let s = norm(v);
    match s.as_str() {
        "somolu" => "shomolu".into(),
        "oworoshonki" => "oworonshoki".into(),
        _ => s,
    }
}
pub fn prepare_args(args: &Value) -> Value {
    let mut args = args.clone();
    if let Some(o) = args.as_object_mut() {
        for v in o.values_mut() {
            if let Some(s) = v.as_str() {
                *v = json!(s.trim());
            }
        }
        if o.get("intent") == Some(&json!("lookup"))
            && o.get("network").is_none_or(|v| string(v).is_empty())
        {
            o.insert("network".into(), json!("PROVIDER"));
        }
        if o.get("plan")
            .is_some_and(|v| matches!(norm(v).as_str(), "pro max" | "pro max plan"))
        {
            o.insert("plan".into(), json!("Promax"));
        }
    }
    args
}
pub fn preflight(args: &Value) -> Option<Value> {
    let valid = args.as_object().is_some_and(|o| {
        o.iter().all(|(k, v)| {
            data()["props"].get(k).is_some() && v.as_str().is_some_and(|s| s.chars().count() <= 160)
        })
    });
    if !valid {
        return Some(result(
            "needs_input",
            "Could you tell me your plan and the area you would like me to check?",
            json!({"missing_fields":["valid_filters"]}),
        ));
    }
    match string(&args["intent"]) {
        "benefits"=>return Some(result("unsupported","I can help you find hospitals, but I don't have your plan's benefit schedule or cover limits here. Leadway's support team can confirm those for you.",json!({"topic":"policy_benefits"}))),
        "conversation"=>return Some(result("ok","Conversational turn only. No provider facts were checked; do not state provider facts.",json!({}))),
        "lookup"=>{}, _=>return Some(result("needs_input","What would you like me to help you check?",json!({"missing_fields":["intent"]})))
    }
    if !data()["networks"]
        .as_array()
        .is_some_and(|a| a.contains(&args["network"]))
    {
        return Some(result(
            "failed",
            "I'm having trouble checking right now. Please let me try again in a moment.",
            json!({"error_type":"invalid_internal_sheet"}),
        ));
    }
    if matches!(string(&args["network"]), "PROVIDER" | "Gyms")
        && string(&args["plan"]).is_empty()
        && string(&args["provider"]).is_empty()
    {
        return Some(result(
            "needs_input",
            "Which health plan are you on?",
            json!({"missing_fields":["plan"]}),
        ));
    }
    if !["provider", "town", "area", "state", "country"]
        .iter()
        .any(|k| !string(&args[*k]).is_empty())
    {
        return Some(result(
            "needs_input",
            "Which area or town would you like me to check?",
            json!({"missing_fields":["location"]}),
        ));
    }
    None
}
fn short(text: &str) -> String {
    let text = n(&text.replace(['\'', '’'], ""));
    let words = text
        .split_whitespace()
        .skip_while(|w| matches!(*w, "then" | "and" | "um" | "uh" | "okay" | "ok" | "please"));
    words.collect::<Vec<_>>().join(" ")
}
pub fn mentioned(text: &str, canonical: bool) -> BTreeSet<String> {
    let heard = n(text);
    let short = short(text);
    let mut vocabulary: BTreeMap<String, String> = data()["town_hearings"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, v)| (k.clone(), string(v).into()))
        .collect();
    if canonical {
        for v in data()["canonical_areas"].as_array().into_iter().flatten() {
            vocabulary.insert(norm(v), string(v).into());
        }
    }
    let cues = heard.split_whitespace().any(|w| {
        matches!(
            w,
            "in" | "around" | "near" | "area" | "location" | "try" | "mention" | "called"
        )
    }) || heard.contains("check for");
    vocabulary
        .into_iter()
        .filter_map(|(alias, target)| {
            let standalone = short == alias
                || short.strip_prefix(&alias).is_some_and(|tail| {
                    !tail.is_empty() && tail.split(&format!(" {alias}")).all(str::is_empty)
                });
            ((format!(" {heard} ").contains(&format!(" {alias} "))) && (cues || standalone))
                .then_some(target)
        })
        .collect()
}
pub fn new_location(text: &str, filters: &Value) -> bool {
    let found = mentioned(text, true);
    found.len() == 1
        && found
            .first()
            .is_some_and(|v| n(v) != norm(&filters["town"]))
}
fn confirmation(args: &Value, changes: Value) -> Value {
    let mut proposed = args.clone();
    if let Some(o) = proposed.as_object_mut() {
        for (k, v) in changes.as_object().into_iter().flatten() {
            if v.is_null() {
                o.remove(k);
            } else {
                o.insert(k.clone(), v.clone());
            }
        }
    }
    let mut pieces = Vec::new();
    if let Some(t) = changes["town"].as_str() {
        pieces.push(format!(
            "{t}{}",
            if norm(&proposed["state"]) == "lagos" {
                " in Lagos"
            } else {
                ""
            }
        ));
    }
    if let Some(p) = changes["plan"].as_str() {
        pieces.push(format!("the {p} plan"));
    }
    result(
        "needs_confirmation",
        &format!("Just to confirm, did you mean {}?", pieces.join(" and ")),
        json!({"suggested_filters":proposed,"corrections":changes,"requires_caller_confirmation":true}),
    )
}
pub fn clarify(args: &Value, transcript: &str) -> Option<Value> {
    if args["intent"] != "lookup" {
        return None;
    }
    let mut changes = json!({});
    let plan = norm(&args["plan"]);
    let plan = plan.strip_suffix(" plan").unwrap_or(&plan);
    let heard = n(transcript);
    for (alias, target) in data()["plan_hearings"].as_object().into_iter().flatten() {
        if plan == alias || format!(" {heard} ").contains(&format!(" {alias} plan ")) {
            changes["plan"] = target.clone();
        }
    }
    if matches!(norm(&args["state"]).as_str(), "" | "lagos") {
        if data()["unclear_fragments"]
            .as_array()
            .is_some_and(|a| a.contains(&json!(short(transcript))))
        {
            return Some(result("needs_input","I didn't quite catch the area. Could you repeat the name slowly, spell it, or give a nearby landmark?",json!({"unclear_location":true})));
        }
        let mut targets = mentioned(transcript, false);
        for field in ["town", "provider"] {
            if let Some(v) = data()["town_hearings"].get(norm(&args[field])) {
                targets.insert(string(v).into());
            }
        }
        if targets.len() > 1 {
            return Some(result(
                "needs_input",
                "I heard more than one possible area. Which area would you like me to check first?",
                json!({"town_suggestions":targets}),
            ));
        }
        if let Some(t) = targets.first() {
            changes["town"] = json!(t);
            changes["state"] = json!("Lagos");
            if data()["town_hearings"]
                .get(norm(&args["provider"]))
                .is_some()
            {
                changes["provider"] = Value::Null;
            }
        }
    }
    (!changes.as_object()?.is_empty()).then(|| confirmation(args, changes))
}
fn fuzzy(value: &str, known: impl IntoIterator<Item = String>) -> Vec<String> {
    let normalized = n(value);
    let compact = normalized.replace(' ', "");
    if compact.len() < 4 {
        return vec![];
    }
    let labels: BTreeMap<String, String> = known
        .into_iter()
        .filter(|v| !n(v).is_empty())
        .map(|v| (n(&v), v.trim().into()))
        .collect();
    if labels.contains_key(&normalized) {
        return vec![];
    }
    let mut scores: Vec<_> = labels
        .into_iter()
        .map(|(k, v)| (super::text::similarity(&compact, &k.replace(' ', "")), v))
        .collect();
    scores.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| b.1.cmp(&a.1)));
    scores
        .into_iter()
        .take(2)
        .filter(|(s, _)| *s >= 0.78)
        .map(|(_, v)| v)
        .collect()
}
fn title(s: &str) -> String {
    s.split_whitespace()
        .map(|w| {
            let mut c = w.chars();
            c.next()
                .map(|v| v.to_uppercase().collect::<String>() + c.as_str())
                .unwrap_or_default()
        })
        .collect::<Vec<_>>()
        .join(" ")
}
pub fn validate_labels(args: &Value, records: &[Value]) -> Option<Value> {
    if !string(&args["plan"]).is_empty() && matches!(string(&args["network"]), "PROVIDER" | "Gyms")
    {
        let plans: BTreeSet<String> = records
            .iter()
            .map(|r| norm(&r["plan"]))
            .map(|s| {
                s.strip_suffix(" and higher")
                    .or_else(|| s.strip_suffix(" higher"))
                    .unwrap_or(&s)
                    .into()
            })
            .filter(|s: &String| !s.is_empty() && s != "all plans")
            .collect();
        let p = norm(&args["plan"]);
        let p = p.strip_suffix(" plan").unwrap_or(&p);
        if !plans.contains(p) {
            let choices = fuzzy(p, plans);
            if choices.len() == 1 {
                return Some(confirmation(args, json!({"plan":title(&choices[0])})));
            }
            return Some(result(
                "needs_input",
                "I couldn't recognise that plan name. Please confirm the name on your plan.",
                json!({"plan_suggestions":choices,"unknown_plan":args["plan"]}),
            ));
        }
    }
    if !string(&args["town"]).is_empty() {
        let candidates: Vec<_> = records
            .iter()
            .filter(|r| {
                string(&args["state"]).is_empty() || norm(&r["state"]) == norm(&args["state"])
            })
            .collect();
        let known: BTreeSet<_> = candidates.iter().map(|r| town(&r["town"])).collect();
        let t = town(&args["town"]);
        if !known.contains(&t)
            && !known.iter().any(|k| {
                t.split_whitespace()
                    .all(|w| k.split_whitespace().any(|v| v == w))
            })
            && data()["nearby"].get(&t).is_none()
        {
            let choices = fuzzy(&t, candidates.iter().map(|r| string(&r["town"]).into()));
            if choices.len() == 1 {
                return Some(confirmation(args, json!({"town":title(&choices[0])})));
            }
            return Some(result("needs_input","Could you confirm the area name or a nearby landmark? I don't want to search the wrong place.",json!({"town_suggestions":choices,"unknown_town":args["town"]})));
        }
    }
    None
}
pub fn records(network: &str, rows: &[Value]) -> Vec<Value> {
    let mut output = vec![];
    let (mut lga, mut lcda) = (String::new(), String::new());
    for (index, row) in rows
        .iter()
        .enumerate()
        .skip(if network == "PHCs" { 2 } else { 1 })
    {
        let r = |i: usize| row.get(i).cloned().unwrap_or_else(|| json!(""));
        if row
            .as_array()
            .is_none_or(|v| v.iter().all(|c| c.is_null() || c == ""))
        {
            continue;
        }
        let keys = match network {
            "PROVIDER" => vec![
                ("state", 2),
                ("town", 3),
                ("provider", 4),
                ("address", 5),
                ("category", 6),
                ("plan", 7),
                ("specialty", 8),
            ],
            "FBN"
                if !string(&r(0)).trim().is_empty()
                    && string(&r(0)).trim().chars().all(char::is_numeric) =>
            {
                vec![
                    ("state", 2),
                    ("town", 3),
                    ("provider", 4),
                    ("address", 5),
                    ("category", 6),
                    ("plan", 7),
                    ("specialty", 8),
                ]
            }
            "FBN" => vec![
                ("state", 0),
                ("town", 1),
                ("provider", 2),
                ("address", 3),
                ("specialty", 4),
            ],
            "MRCare 1" | "MRCare 2" => vec![
                ("state", 2),
                ("town", 3),
                ("provider", 4),
                ("address", 5),
                ("specialty", 6),
            ],
            "Gyms" => vec![
                ("state", 1),
                ("town", 2),
                ("provider", 3),
                ("address", 4),
                ("plan", 5),
            ],
            "Cromwell" => vec![("provider", 0), ("town", 1), ("address", 1), ("country", 2)],
            "i-Fitness Branches" => vec![("town", 0), ("address", 1)],
            "PHCs" => vec![("provider", 5), ("address", 6), ("facility_status", 7)],
            _ => vec![],
        };
        let mut out = json!({"sheet":network,"row":index+1});
        for (k, i) in keys {
            out[k] = r(i);
        }
        if network == "i-Fitness Branches" {
            out["provider"] = json!(format!("i-Fitness {}", string(&r(0))));
        }
        if network == "PHCs" {
            if !string(&r(2)).is_empty() {
                lga = string(&r(2)).into();
            }
            if !string(&r(3)).is_empty() {
                lcda = string(&r(3)).into();
            }
            out["state"] = json!("Lagos");
            out["town"] = json!(format!("{lga} {lcda} {}", string(&r(4))));
        }
        if !string(&out["provider"]).trim().is_empty() {
            output.push(out);
        }
    }
    output
}
fn score(r: &Value, a: &Value) -> u32 {
    let mut score = 0;
    if !string(&a["state"]).is_empty() && norm(&r["state"]) == norm(&a["state"]) {
        score += 20;
    }
    if !string(&a["town"]).is_empty() {
        let t = town(&a["town"]);
        score += if town(&r["town"]) == t {
            30
        } else if locality(&t, &[string(&r["town"])]) {
            20
        } else if locality(&t, &[string(&r["address"])]) {
            10
        } else {
            0
        };
    }
    if !string(&a["area"]).is_empty() {
        score += if locality(string(&a["area"]), &[string(&r["address"])]) {
            40
        } else if locality(string(&a["area"]), &[string(&r["town"])]) {
            25
        } else {
            0
        };
    }
    if !string(&a["specialty"]).is_empty() {
        let wanted = specialty(string(&a["specialty"]));
        score += if locality(&wanted, &[string(&r["specialty"])]) {
            30
        } else if locality(&wanted, &[string(&r["category"])]) {
            25
        } else if locality(&wanted, &[string(&r["provider"])]) {
            10
        } else {
            0
        };
    }
    score
}
fn specialty(q: &str) -> String {
    data()["specialties"]
        .get(n(q))
        .map(norm)
        .unwrap_or_else(|| n(q))
}
pub fn matches(network: &str, rows: &[Value], args: &Value, delisted: &[Value]) -> Vec<Value> {
    let mut out: Vec<_> = records(network, rows)
        .into_iter()
        .filter(|r| {
            if ["state", "provider", "country"].iter().any(|k| {
                !string(&args[*k]).is_empty() && !contains(string(&args[*k]), string(&r[*k]))
            }) {
                return false;
            }
            if !string(&args["town"]).is_empty()
                && !locality(
                    &town(&args["town"]),
                    &[&town(&r["town"]), string(&r["address"])],
                )
            {
                return false;
            }
            if !string(&args["area"]).is_empty()
                && !locality(
                    string(&args["area"]),
                    &[string(&r["town"]), string(&r["address"])],
                )
            {
                return false;
            }
            if !string(&args["specialty"]).is_empty()
                && !["specialty", "category", "provider"].iter().any(|k| {
                    !string(&r[*k]).is_empty()
                        && contains(&specialty(string(&args["specialty"])), string(&r[*k]))
                })
            {
                return false;
            }
            if !string(&args["plan"]).is_empty() && matches!(network, "PROVIDER" | "Gyms") {
                let p = norm(&args["plan"]);
                let p = p.strip_suffix(" plan").unwrap_or(&p);
                let label = norm(&r["plan"]);
                if ![
                    "all plans".into(),
                    p.into(),
                    format!("{p} higher"),
                    format!("{p} and higher"),
                ]
                .contains(&label)
                {
                    return false;
                }
            }
            if network == "FBN"
                && delisted
                    .iter()
                    .skip(1)
                    .filter_map(|r| r.get(0).and_then(Value::as_str))
                    .any(|x| {
                        !x.trim().is_empty()
                            && (n(x) == norm(&r["provider"])
                                || contains(x, string(&r["provider"]))
                                || contains(string(&r["provider"]), x))
                    })
            {
                return false;
            }
            true
        })
        .collect();
    out.sort_by_key(|r| {
        (
            std::cmp::Reverse(score(r, args)),
            norm(&r["provider"]),
            norm(&r["address"]),
        )
    });
    out
}

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub endpoint: String,
    pub token: String,
    pub contacts_db: String,
    pub zepto_key: String,
    pub from_address: String,
    pub allow_loopback: bool,
    pub mail_url: String,
}
impl Config {
    pub fn validate(&self) -> Result<(), String> {
        if !self.endpoint.is_empty() && !source_url(&self.endpoint, self.allow_loopback) {
            return Err("directory requires the operator's Google Apps Script endpoint".into());
        }
        if !self.mail_url.is_empty()
            && self.mail_url != "https://cpaas.zoho.com/v1.1/email"
            && !(self.allow_loopback && loopback(&self.mail_url))
        {
            return Err("unsupported directory email endpoint".into());
        }
        Ok(())
    }
}
fn loopback(s: &str) -> bool {
    reqwest::Url::parse(s).is_ok_and(|u| {
        u.scheme() == "http"
            && matches!(u.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"))
            && u.username().is_empty()
            && u.password().is_none()
    })
}
fn source_url(s: &str, local: bool) -> bool {
    (s.starts_with("https://script.google.com/macros/s/")
        && s.ends_with("/exec")
        && reqwest::Url::parse(s).is_ok_and(|u| {
            u.username().is_empty()
                && u.password().is_none()
                && u.query().is_none()
                && u.fragment().is_none()
        }))
        || (local && loopback(s))
}
#[derive(Clone)]
pub struct Client {
    config: Arc<Config>,
    scope: VerifiedSession,
    http: reqwest::Client,
    mail: reqwest::Client,
    session: String,
}
impl Client {
    pub fn new(config: Config, scope: VerifiedSession, session: String) -> Result<Self, String> {
        config.validate()?;
        let local = config.allow_loopback;
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::custom(move |a| {
                if a.previous().len() >= 5 {
                    return a.error("too many source redirects");
                }
                let u = a.url();
                if u.scheme() == "https"
                    && matches!(
                        u.host_str(),
                        Some("script.google.com" | "script.googleusercontent.com")
                    )
                    || local && loopback(u.as_str())
                {
                    a.follow()
                } else {
                    a.stop()
                }
            }))
            .build()
            .map_err(|_| "directory client initialization failed")?;
        let mail = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| "directory mail initialization failed")?;
        Ok(Self {
            config: Arc::new(config),
            scope,
            http,
            mail,
            session,
        })
    }
    pub async fn lookup(&self, args: &Value) -> Value {
        self.lookup_limit(args, 5).await
    }
    async fn lookup_limit(&self, args: &Value, limit: usize) -> Value {
        let args = prepare_args(args);
        if let Some(p) = preflight(&args) {
            return p;
        }
        if self.config.token.is_empty()
            || !source_url(&self.config.endpoint, self.config.allow_loopback)
        {
            return result(
                "failed",
                "The live directory connection is unavailable. Do not use old results.",
                json!({}),
            );
        }
        match tokio::time::timeout(Duration::from_secs(25),self.read(&args,limit)).await {Ok(Ok(v))=>v,_=>result("failed","I cannot check the current hospital information right now. This is a connection problem, not evidence that the hospital or location is wrong. Do not give facts from previous results.",json!({"error_type":"SourceUnavailable"}))}
    }
    async fn read(&self, args: &Value, limit: usize) -> Result<Value, String> {
        let network = string(&args["network"]);
        let mut names = vec![network];
        if network == "FBN" {
            names.push("DELISTED FBN 05-05-26");
        }
        let mut body = Value::Null;
        for attempt in 0..2 {
            let response = self
                .http
                .post(&self.config.endpoint)
                .timeout(Duration::from_secs(12))
                .json(&json!({"token":self.config.token,"sheets":names}))
                .send()
                .await;
            let response = match response {
                Ok(r) => {
                    if r.status().is_client_error() {
                        return Err("source rejected".into());
                    }
                    r.error_for_status().ok()
                }
                Err(_) => None,
            };
            let decoded = match response {
                Some(r) => bounded_json(r, 8 * 1024 * 1024).await.ok(),
                None => None,
            };
            if let Some(v) = decoded {
                if v["error"] != "SOURCE_UNAVAILABLE" {
                    body = v;
                    break;
                }
            }
            if attempt == 1 {
                return Err("source unavailable".into());
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        evaluate(args, &body, limit)
    }
}
async fn bounded_json(response: reqwest::Response, limit: usize) -> Result<Value, String> {
    let mut bytes = vec![];
    let mut chunks = response.bytes_stream();
    while let Some(part) = chunks.next().await {
        let part = part.map_err(|_| "source response failed")?;
        if bytes.len() + part.len() > limit {
            return Err("source response too large".into());
        }
        bytes.extend_from_slice(&part);
    }
    serde_json::from_slice(&bytes).map_err(|_| "source JSON invalid".into())
}
pub fn evaluate(args: &Value, body: &Value, limit: usize) -> Result<Value, String> {
    if body["ok"] != true || body["spreadsheetId"] != data()["sheet_id"] {
        return Err("invalid source".into());
    }
    let network = string(&args["network"]);
    let sheets = body["sheets"].as_array().ok_or("missing sheets")?;
    let sheet = |name: &str| {
        sheets
            .iter()
            .find(|s| s["name"] == name)
            .and_then(|s| s["values"].as_array())
            .filter(|s| !s.is_empty())
            .ok_or("missing sheet".to_string())
    };
    let rows = sheet(network)?;
    let expected = match network {
        "PROVIDER" => vec![
            "CODE",
            "ZONE",
            "STATE",
            "TOWN",
            "PROVIDER",
            "ADDRESS",
            "CAT",
            "PLAN",
            "SPECIALTY",
        ],
        "FBN" => vec!["STATE", "TOWN", "PROVIDER", "ADDRESS", "SPECIALTY"],
        "Gyms" => vec!["CODE", "STATE", "TOWN", "NAME", "ADDRESS", "PLAN"],
        "MRCare 1" | "MRCare 2" => vec![
            "CODE",
            "ZONE",
            "STATE",
            "TOWN",
            "PROVIDER",
            "ADDRESS",
            "SPECIALTY",
        ],
        "Cromwell" => vec!["PROVIDER", "CITY", "COUNTRY"],
        "i-Fitness Branches" => vec![
            "BRANCH",
            "ADDRESS",
            "CONTACT PERSON",
            "DESIGNATION",
            "EMAIL",
            "MOBILE NUMBER",
        ],
        "PHCs" => vec![
            "S/N",
            "S/N OF FACILITY",
            "LGA",
            "LGA/LCDA",
            "WARD",
            "NAME OF H/F",
            "ADDRESS",
            "STATUS",
        ],
        _ => return Err("invalid sheet".into()),
    };
    let header = rows
        .get(usize::from(network == "PHCs"))
        .and_then(Value::as_array)
        .ok_or("missing columns")?;
    if header.len() < expected.len()
        || expected
            .iter()
            .enumerate()
            .any(|(i, e)| string(&header[i]).trim() != *e)
    {
        return Err("columns changed".into());
    }
    let delisted = if network == "FBN" {
        let d = sheet("DELISTED FBN 05-05-26")?;
        if d[0].get(0) != Some(&json!("Provider Name")) {
            return Err("exclusions changed".into());
        }
        d.as_slice()
    } else {
        &[]
    };
    let rs = records(network, rows);
    if let Some(v) = validate_labels(args, &rs) {
        return Ok(v);
    }
    let mut found = matches(network, rows, args, delisted);
    let total = found.len();
    found.truncate(limit.clamp(1, 7));
    let mut nearby = vec![];
    if total == 0
        && norm(&args["state"]) == "lagos"
        && !string(&args["town"]).is_empty()
        && string(&args["provider"]).is_empty()
    {
        for t in data()["nearby"][norm(&args["town"])]
            .as_array()
            .into_iter()
            .flatten()
        {
            let mut a = args.clone();
            a["town"] = t.clone();
            let f = matches(network, rows, &a, delisted);
            if !f.is_empty() {
                nearby.push(json!({"town":t,"total_matches":f.len(),"matches":f.into_iter().take(2).collect::<Vec<_>>(),"filters":a}));
            }
        }
        nearby.truncate(3);
    }
    Ok(result("ok","Current directory listings only; individual coverage is not verified. Plan matching uses explicit labels only; no tier hierarchy is assumed. Location and specialty matches are based on sheet town, address, category, specialty and provider fields. Results are relevance-ranked, not distance-ranked. No matches means none match these filters, not a definitive coverage denial.",json!({"network":network,"filters":args,"matches":found,"total_matches":total,"nearby_options":nearby,"read_completed_at":body["readCompletedAt"],"source":"live_google_sheet"})))
}
pub async fn decision(
    p: &Providers,
    text: &str,
    pending: &Pending,
    history: Vec<Value>,
    action: &str,
) -> Decision {
    let messages = vec![
        json!({"role":"system","content":data()["confirmation_policy"]}),
        json!({"role":"user","content":super::python_json(&json!({"action":action,"recent_conversation":history.into_iter().filter(|m|matches!(string(&m["role"]),"user"|"assistant")).rev().take(6).collect::<Vec<_>>().into_iter().rev().map(|m|json!({"role":m["role"],"content":string(&m["content"]).chars().take(1500).collect::<String>()})).collect::<Vec<_>>(),"pending_confirmation":pending.question,"current_caller_reply":text}))}),
    ];
    match tokio::time::timeout(Duration::from_secs(6), p.collect(messages, 8, 0.0)).await {
        Ok(Ok(s)) => Decision::parse(&s),
        _ => Decision::Unclear,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixtures() -> Value {
        serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json")).unwrap()
    }
    #[test]
    fn pure_contracts_match_unchanged_python() {
        let f = fixtures();
        assert_eq!(data(), &f["directory_data"]);
        for c in f["directory_pure"].as_array().unwrap() {
            let a = prepare_args(&c["args"]);
            assert_eq!(a, c["prepared"]);
            assert_eq!(preflight(&a).unwrap_or(Value::Null), c["preflight"], "{c}");
            assert_eq!(
                clarify(&a, string(&c["transcript"])).unwrap_or(Value::Null),
                c["clarify"],
                "{c}"
            );
            assert_eq!(
                json!(mentioned(string(&c["transcript"]), true)),
                c["mentioned"]
            );
        }
    }
    #[test]
    fn rows_ranking_and_labels_match_unchanged_python() {
        let f = fixtures();
        for c in f["directory_rows"].as_array().unwrap() {
            assert_eq!(
                json!(records(
                    string(&c["network"]),
                    c["rows"].as_array().unwrap()
                )),
                c["expected"]
            );
        }
        for c in f["directory_matches"].as_array().unwrap() {
            let rs = records(string(&c["network"]), c["rows"].as_array().unwrap());
            assert_eq!(
                json!(matches(
                    string(&c["network"]),
                    c["rows"].as_array().unwrap(),
                    &c["args"],
                    c["delisted"].as_array().unwrap()
                )),
                c["expected"],
                "{c}"
            );
            assert_eq!(
                validate_labels(&c["args"], &rs).unwrap_or(Value::Null),
                c["labels"],
                "{c}"
            );
        }
    }
    #[test]
    fn fresh_source_identity_headers_nearby_and_result_limits_are_enforced() {
        let f = fixtures();
        let mut rows = f["directory_rows"][0]["rows"].clone();
        let a = json!({"intent":"lookup","network":"PROVIDER","plan":"Plus","state":"Lagos","town":"Ikeja"});
        for i in 0..12 {
            rows.as_array_mut().unwrap().push(json!([
                i.to_string(),
                "",
                "Lagos",
                "Ikeja",
                format!("Test {i}"),
                "Road",
                "D",
                "PLUS & HIGHER",
                "DENTAL"
            ]));
        }
        let mut body = json!({"ok":true,"spreadsheetId":data()["sheet_id"],"readCompletedAt":"fresh","sheets":[{"name":"PROVIDER","values":rows}]});
        assert_eq!(
            evaluate(&a, &body, 7).unwrap()["data"]["matches"]
                .as_array()
                .unwrap()
                .len(),
            7
        );
        let nearby=evaluate(&json!({"intent":"lookup","network":"PROVIDER","plan":"Plus","state":"Lagos","town":"Lagos Island"}),&body,5).unwrap();
        assert_eq!(nearby["data"]["matches"], json!([]));
        assert_eq!(nearby["data"]["nearby_options"][0]["town"], "Ikoyi");
        assert_eq!(
            nearby["data"]["nearby_options"][0]["filters"]["plan"],
            "Plus"
        );
        body["spreadsheetId"] = json!("wrong");
        assert!(evaluate(&a, &body, 5).is_err());
        body["spreadsheetId"] = data()["sheet_id"].clone();
        body["sheets"][0]["values"][0][0] = json!("CHANGED COLUMN");
        assert!(evaluate(&a, &body, 5).is_err());
    }
}
