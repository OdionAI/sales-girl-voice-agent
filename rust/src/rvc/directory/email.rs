use super::*;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use sha2::{Digest, Sha256};

#[derive(Clone, Debug)]
pub struct Contact {
    pub phone: String,
    pub email: String,
    pub full_name: String,
}
pub struct Prepared {
    contact: Contact,
    recipient: String,
    filters: Value,
    alternate: bool,
}
pub struct Prepare {
    pub body: Value,
    pub prepared: Option<Prepared>,
    pub needs_search: bool,
}
fn valid_email(s: &str) -> bool {
    s.len() <= 254 && regex::Regex::new(r"^[^\s@]+@[^\s@]+\.[^\s@]+$").is_ok_and(|r| r.is_match(s))
}
fn casefold(s: &str) -> String {
    s.chars()
        .map(|c| {
            data()["casefold_map"]
                .get((c as u32).to_string())
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| c.to_string())
        })
        .collect()
}
pub fn normalize_phone(input: &str) -> String {
    static DIGITS: OnceLock<Value> = OnceLock::new();
    let digits = DIGITS.get_or_init(|| {
        serde_json::from_str(include_str!("../unicode_decimals.json")).unwrap_or(Value::Null)
    });
    let raw = input.trim();
    let raw = raw.strip_prefix('+').unwrap_or(raw);
    let is_digit = |c: char| digits.get((c as u32).to_string()).is_some();
    if raw.is_empty()
        || !raw
            .chars()
            .all(|c| is_digit(c) || matches!(c, ' ' | '(' | ')' | '-'))
    {
        return String::new();
    }
    let mut value: String = raw.chars().filter(|c| is_digit(*c)).collect();
    if value.starts_with("00") {
        value = value[2..].into();
    }
    if value.len() == 11 && value.starts_with('0') {
        value = format!("234{}", &value[1..]);
    }
    if !(10..=15).contains(&value.chars().count()) || value.starts_with('0') {
        String::new()
    } else {
        format!("+{value}")
    }
}
pub fn speech_text(input: &str) -> String {
    let mut out = regex::Regex::new("(?i)https?://")
        .map(|r| r.replace_all(input, "").into_owned())
        .unwrap_or_else(|_| input.into());
    let chars: Vec<_> = out.chars().collect();
    out = chars
        .iter()
        .enumerate()
        .map(|(i, c)| {
            if *c == '.'
                && i > 0
                && i + 1 < chars.len()
                && (chars[i - 1].is_alphanumeric() || chars[i - 1] == '_')
                && (chars[i + 1].is_alphanumeric() || chars[i + 1] == '_')
            {
                " dot ".into()
            } else {
                c.to_string()
            }
        })
        .collect();
    for (symbol, word) in [
        ('#', " hash "),
        ('*', " star "),
        ('@', " at "),
        ('&', " and "),
    ] {
        out = out.replace(symbol, word);
    }
    out
}
impl Prepared {
    pub fn alternate(&self) -> bool {
        self.alternate
    }
}
impl Client {
    fn read_contact(&self) -> Result<Option<Contact>, String> {
        let c = self.scope.claims();
        if c["entry_surface"] != "aicc_inbound"
            || c["sip_caller_verified"] != true
            || string(&c["sip_trunk_id"]).trim().is_empty()
            || c["business_id"] != self.scope.business_id()
            || c["config_agent_id"] != self.scope.agent_id()
        {
            return Ok(None);
        }
        let phone = normalize_phone(string(&c["caller_phone"]));
        if phone.is_empty() || self.config.contacts_db.is_empty() {
            return Ok(None);
        }
        let db =
            Connection::open_with_flags(&self.config.contacts_db, OpenFlags::SQLITE_OPEN_READ_ONLY)
                .map_err(|_| "saved contacts unavailable")?;
        db.busy_timeout(Duration::from_secs(5))
            .map_err(|_| "saved contacts unavailable")?;
        db.query_row("SELECT email,full_name FROM caller_contacts WHERE business_id=? AND phone_e164=? AND active=1",params![self.scope.business_id(),phone],|r|Ok(Contact{phone:phone.clone(),email:r.get::<_,String>(0)?.trim().into(),full_name:r.get(1)?})).optional().map_err(|_|"saved contacts unavailable".into())
    }
    pub async fn contact(&self) -> Result<Option<Contact>, String> {
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.read_contact())
            .await
            .map_err(|_| "saved contacts unavailable")?
    }
    pub async fn prepare(&self, arguments: &Value, filters: Option<Value>) -> Prepare {
        let error = |status: &str, message: &str| Prepare {
            body: result(status, message, json!({})),
            prepared: None,
            needs_search: false,
        };
        if !arguments.as_object().is_some_and(|o| {
            o.keys().all(|k| k == "recipient_email")
                && o.get("recipient_email").is_none_or(Value::is_string)
        }) {
            return error(
                "failed",
                "The email tool accepts only an optional destination email address.",
            );
        }
        let contact=match self.contact().await{Ok(Some(c))=>c,Ok(None)=>return error("needs_input","I can't find a saved contact for this calling number. It needs to be added to the contact list before I can email the list."),Err(_)=>return error("failed","I can't access the saved contact list right now.")};
        let Some(filters) = filters else {
            let mut r = error(
                "needs_input",
                "Let’s find the hospitals you need first. Which plan and location should I check?",
            );
            r.needs_search = true;
            return r;
        };
        let alternate = string(&arguments["recipient_email"]).trim();
        if !alternate.is_empty() && !valid_email(alternate) {
            return error(
                "needs_input",
                "I didn't catch a valid email address. Please say the address again.",
            );
        }
        if alternate.is_empty() && contact.email.is_empty() {
            return error("needs_input","I don't have an email saved for this number yet. Please provide the address you'd like me to use for this hospital list.");
        }
        let different = !alternate.is_empty() && casefold(alternate) != casefold(&contact.email);
        let recipient = if different {
            alternate.to_owned()
        } else {
            contact.email.clone()
        };
        if !valid_email(&recipient) {
            return error(
                "failed",
                "I can't verify a valid saved destination right now.",
            );
        }
        let body = if different {
            result("needs_confirmation",&format!("You asked me to send the hospital list to {}, instead of your saved email. Is that the correct address? I will send it there only after you confirm.",speech_text(&recipient).trim()),json!({}))
        } else {
            result(
                "ready",
                "I’ll send that to the email we have for you.",
                json!({}),
            )
        };
        Prepare {
            body,
            prepared: Some(Prepared {
                contact,
                recipient,
                filters,
                alternate: different,
            }),
            needs_search: false,
        }
    }
    /// The caller's saved-address request is final-authorized by the actor.
    /// An alternate consumes a separate exact-destination confirmation permit.
    pub async fn send(&self, prepared: Prepared, permit: Option<Permit>) -> ActionResult {
        if prepared.alternate {
            let Some(permit) = permit else {
                return ActionResult::failed(
                    "The alternate email address has not been confirmed by the caller.",
                );
            };
            let call = permit.into_call();
            if call.name != EMAIL
                || string(&call.arguments["recipient_email"]).trim() != prepared.recipient
            {
                return ActionResult::failed(
                    "The confirmed email destination has changed. Please request the email again.",
                );
            }
        }
        let current = match self.contact().await {
            Ok(Some(c)) => c,
            _ => return ActionResult::failed("I can't verify the saved contact right now."),
        };
        if current.phone != prepared.contact.phone || current.email != prepared.contact.email {
            return ActionResult::failed(
                "The saved contact has changed. Please request the email again.",
            );
        }
        if self.config.zepto_key.is_empty() || !valid_email(&self.config.from_address) {
            return ActionResult::failed("Email sending is not available right now.");
        }
        let fresh = self.lookup_limit(&prepared.filters, 7).await;
        if fresh["status"] != "ok" {
            return ActionResult::failed("I couldn't refresh the hospital list just now, so the email hasn't gone out. That doesn't mean the hospitals are no longer listed. Would you like me to try again?");
        }
        let rows = fresh["data"]["matches"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        if rows.is_empty() {
            return ActionResult::from_body(result("needs_input","The latest check didn't return hospitals for that search, so I haven't emailed an empty list. Let's check the plan and area again.",json!({})));
        }
        let operation = uuid::Uuid::new_v4().to_string();
        let hash = search_hash(&prepared.filters, &prepared.recipient);
        let (client, op, source, count, phone, recipient) = (
            self.clone(),
            operation.clone(),
            fresh["data"]["read_completed_at"]
                .as_str()
                .map(str::to_owned),
            rows.len(),
            prepared.contact.phone.clone(),
            prepared.recipient.clone(),
        );
        let reservation = tokio::task::spawn_blocking(move || {
            client.reserve(&op, &hash, &phone, &recipient, source, count)
        })
        .await;
        match reservation{
            Ok(Ok(Some(status))) if status=="accepted"=>return ActionResult::from_body(result("success","That list is already on its way to your email. Please check your inbox or spam folder.",json!({"deduplicated":true}))),
            Ok(Ok(Some(_)))=>return ActionResult::from_body(result("unknown","An email attempt for this list is already recorded. I can't confirm delivery, so I won't risk sending a duplicate.",json!({}))),
            Ok(Ok(None))=>{},_=>return ActionResult::failed("I couldn't record this email request safely, so I haven't submitted it.")
        }
        let (text, html) = render_email(&fresh["data"], &prepared.contact.full_name);
        let payload = json!({"from":{"address":self.config.from_address,"name":"Amaka — Leadway Health"},"to":[{"email_address":{"address":prepared.recipient}}],"subject":"Your requested hospital list — Leadway Health","textbody":text,"htmlbody":html,"client_reference":operation,"track_opens":false,"track_clicks":false});
        let url = if self.config.mail_url.is_empty() {
            "https://cpaas.zoho.com/v1.1/email"
        } else {
            &self.config.mail_url
        };
        let response = self
            .mail
            .post(url)
            .timeout(Duration::from_secs(25))
            .header(
                "Authorization",
                format!("Zoho-enczapikey {}", self.config.zepto_key),
            )
            .json(&payload)
            .send()
            .await;
        let (status, request) = match response {
            Ok(r) => {
                let code = r.status();
                match bounded_json(r, 256 * 1024).await {
                    Ok(body) => {
                        let accepted = code.is_success()
                            && body.is_object()
                            && body
                                .get("error")
                                .is_none_or(|v| v.is_null() || v == false || v == "")
                            && body["data"]
                                .as_array()
                                .is_some_and(|a| a.iter().any(|d| d["code"] == "EM_104"));
                        (
                            if accepted { "accepted" } else { "failed" },
                            string(&body["request_id"])
                                .chars()
                                .take(255)
                                .collect::<String>(),
                        )
                    }
                    Err(_) => ("unknown", String::new()),
                }
            }
            Err(_) => ("unknown", String::new()),
        };
        let (client, id, request_id) = (self.clone(), operation.clone(), request.clone());
        let recorded =
            tokio::task::spawn_blocking(move || client.record(&id, status, &request_id)).await;
        if !matches!(recorded, Ok(Ok(()))) {
            return ActionResult::from_body(result("unknown","The email submission may have completed, but its record could not be confirmed. Please check your inbox; I won't send it again automatically.",json!({"operation_id":operation})));
        }
        let (label,message)=match status{"accepted"=>("success","That's on its way to the email we have for you. It should arrive shortly. Please check your spam folder too."),"failed"=>("failed","The email service couldn't accept the message right now. I can still read the hospital details to you."),_=>("unknown","I couldn't confirm whether the email was accepted. Please check your inbox; I won't send it again automatically.")};
        ActionResult::from_body(result(
            label,
            message,
            json!({"operation_id":operation,"count":rows.len(),"provider_status":status}),
        ))
    }
    fn write_db(&self) -> Result<Connection, String> {
        let db = Connection::open_with_flags(
            &self.config.contacts_db,
            OpenFlags::SQLITE_OPEN_READ_WRITE,
        )
        .map_err(|_| "outbox unavailable")?;
        db.busy_timeout(Duration::from_secs(5))
            .map_err(|_| "outbox busy")?;
        Ok(db)
    }
    fn reserve(
        &self,
        id: &str,
        hash: &str,
        phone: &str,
        recipient: &str,
        source: Option<String>,
        count: usize,
    ) -> Result<Option<String>, String> {
        let mut db = self.write_db()?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS directory_email_outbox(id TEXT PRIMARY KEY,business_id TEXT NOT NULL,agent_id TEXT NOT NULL,session_id TEXT NOT NULL,phone_e164 TEXT NOT NULL,recipient TEXT NOT NULL,search_hash TEXT NOT NULL,status TEXT NOT NULL,provider_request_id TEXT,source_read_at TEXT,result_count INTEGER,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(business_id,agent_id,session_id,search_hash));").map_err(|_|"outbox unavailable")?;
        let tx = db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|_| "outbox busy")?;
        let old=tx.query_row("SELECT status FROM directory_email_outbox WHERE business_id=? AND agent_id=? AND session_id=? AND search_hash=?",params![self.scope.business_id(),self.scope.agent_id(),self.session,hash],|r|r.get::<_,String>(0)).optional().map_err(|_|"outbox unavailable")?;
        if old.is_some() {
            return Ok(old);
        }
        tx.execute("INSERT INTO directory_email_outbox(id,business_id,agent_id,session_id,phone_e164,recipient,search_hash,status,source_read_at,result_count) VALUES(?,?,?,?,?,?,?,'sending',?,?)",params![id,self.scope.business_id(),self.scope.agent_id(),self.session,phone,recipient,hash,source,count as i64]).map_err(|_|"outbox reservation failed")?;
        tx.commit().map_err(|_| "outbox commit failed")?;
        Ok(None)
    }
    fn record(&self, id: &str, status: &str, request: &str) -> Result<(), String> {
        let db = self.write_db()?;
        let n=db.execute("UPDATE directory_email_outbox SET status=?,provider_request_id=? WHERE id=? AND business_id=? AND agent_id=? AND session_id=?",params![status,request,id,self.scope.business_id(),self.scope.agent_id(),self.session]).map_err(|_|"outbox update failed")?;
        if n != 1 {
            return Err("outbox update missing".into());
        }
        Ok(())
    }
}
fn sorted(v: &Value) -> Value {
    match v {
        Value::Object(o) => {
            let sorted: BTreeMap<_, _> = o.iter().map(|(k, v)| (k, sorted(v))).collect();
            json!(sorted)
        }
        Value::Array(a) => json!(a.iter().map(sorted).collect::<Vec<_>>()),
        _ => v.clone(),
    }
}
fn search_hash(filters: &Value, recipient: &str) -> String {
    format!(
        "{:x}",
        Sha256::digest(
            super::super::python_json(&sorted(
                &json!({"filters":filters,"recipient":casefold(recipient)})
            ))
            .as_bytes()
        )
    )
}
fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}
fn render_email(data: &Value, name: &str) -> (String, String) {
    let filters = data["filters"]
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(k, _)| *k != "intent")
        .map(|(k, v)| format!("{k}: {}", string(v)))
        .collect::<Vec<_>>()
        .join(", ");
    let name = name
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(120)
        .collect::<String>();
    let mut lines=vec![if name.is_empty(){"Hello,".into()}else{format!("Hello {name},")},"".into(),"Thank you for speaking with Amaka at Leadway Health. It was a pleasure helping you today.".into(),"Please find the hospital list you requested below. We hope this makes it easier to choose a convenient place for your care.".into(),"".into(),format!("Search: {filters}"),format!("Directory checked: {}",data["read_completed_at"].as_str().filter(|s|!s.is_empty()).unwrap_or("just now")),"".into()];
    for row in data["matches"].as_array().into_iter().flatten().take(7) {
        lines.push(string(&row["provider"]).into());
        for key in [
            "address",
            "town",
            "state",
            "country",
            "plan",
            "specialty",
            "facility_status",
        ] {
            if !string(&row[key]).is_empty() {
                lines.push(format!(
                    "{}: {}",
                    title(&key.replace('_', " ")),
                    string(&row[key])
                ));
            }
        }
        lines.push("".into());
    }
    lines.extend(["These are current directory listings, not confirmation of active policy coverage, preauthorisation or appointment availability.","","If you need help finding another hospital or have any questions, please contact Leadway Health. We will be happy to help.","Thank you for choosing Leadway Health. Wishing you good health and a pleasant day.","","Warm regards,","Amaka","Leadway Health"].map(str::to_owned));
    (
        lines.join("\n"),
        format!(
            "<div style=\"font-family:Arial,sans-serif;line-height:1.6;max-width:720px\">{}</div>",
            lines
                .iter()
                .map(|s| html_escape(s))
                .collect::<Vec<_>>()
                .join("<br>")
        ),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn phone_and_readback_match_python() {
        let f: Value = serde_json::from_str(include_str!(
            "../../../fixtures/python-parity-reference.json"
        ))
        .unwrap();
        for c in f["directory_phones"].as_array().unwrap() {
            assert_eq!(normalize_phone(string(&c["input"])), c["expected"], "{c}");
        }
        for c in f["directory_speech"].as_array().unwrap() {
            assert_eq!(speech_text(string(&c["input"])), c["expected"], "{c}");
        }
    }
}

#[cfg(test)]
mod contract_tests {
    use super::*;
    #[test]
    fn email_content_and_durable_hash_match_unchanged_python() {
        let f: Value = serde_json::from_str(include_str!(
            "../../../fixtures/python-parity-reference.json"
        ))
        .unwrap();
        for c in f["directory_email_contracts"].as_array().unwrap() {
            assert_eq!(
                search_hash(&c["filters"], string(&c["recipient"])),
                c["hash"]
            );
            let (text, html) = render_email(&c["data"], "Ada Test");
            assert_eq!(text, c["text"]);
            assert_eq!(html, c["html"]);
            assert!(!html.contains("Clinic <branch>"));
        }
    }
    fn scope(business: &str) -> VerifiedSession {
        VerifiedSession::from_pinned_sip(json!({"entry_surface":"aicc_inbound","sip_caller_verified":true,"sip_trunk_id":"fixture-trunk","business_id":business,"config_agent_id":"agent-a","end_user_id":"caller-a","caller_phone":"+2348012345678"})).unwrap()
    }
    fn setup() -> (Client, std::path::PathBuf) {
        let path = std::env::temp_dir().join(format!(
            "sg-directory-contract-{}.sqlite",
            uuid::Uuid::new_v4()
        ));
        let db = Connection::open(&path).unwrap();
        db.execute_batch("CREATE TABLE caller_contacts(business_id TEXT,phone_e164 TEXT,email TEXT,full_name TEXT,active INTEGER);INSERT INTO caller_contacts VALUES('business-a','+2348012345678','saved@example.invalid','Ada',1);").unwrap();
        let client = Client::new(
            Config {
                contacts_db: path.to_string_lossy().into(),
                ..Default::default()
            },
            scope("business-a"),
            "session-a".into(),
        )
        .unwrap();
        (client, path)
    }
    #[tokio::test]
    async fn saved_contacts_are_scoped_and_changed_contact_cannot_send() {
        let (client, path) = setup();
        assert_eq!(client.contact().await.unwrap().unwrap().full_name, "Ada");
        let other = Client::new(
            (*client.config).clone(),
            scope("business-b"),
            "session-a".into(),
        )
        .unwrap();
        assert!(other.contact().await.unwrap().is_none());
        let web = Client::new(
            (*client.config).clone(),
            super::super::super::identity::test_session(json!({})),
            "session-a".into(),
        )
        .unwrap();
        assert!(web.contact().await.unwrap().is_none());
        let prepared = client
            .prepare(
                &json!({}),
                Some(json!({"intent":"lookup","network":"PROVIDER","plan":"Plus","town":"Ikeja"})),
            )
            .await
            .prepared
            .unwrap();
        let db = Connection::open(&path).unwrap();
        db.execute(
            "UPDATE caller_contacts SET email='changed@example.invalid'",
            [],
        )
        .unwrap();
        drop(db);
        let outcome = client.send(prepared, None).await;
        assert_eq!(outcome.status, "failed");
        assert!(outcome.message.contains("changed"));
        std::fs::remove_file(path).unwrap();
    }
    #[tokio::test]
    async fn alternate_destination_without_fresh_permit_never_sends() {
        let (client, path) = setup();
        let prep = client
            .prepare(
                &json!({"recipient_email":"other@example.invalid"}),
                Some(json!({"intent":"lookup","network":"PROVIDER","provider":"Clinic"})),
            )
            .await;
        assert_eq!(prep.body["status"], "needs_confirmation");
        assert_eq!(
            client.send(prep.prepared.unwrap(), None).await.status,
            "failed"
        );
        std::fs::remove_file(path).unwrap();
    }
}
