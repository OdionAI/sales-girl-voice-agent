//! Call-scoped WebAuthn demonstration. Its outcome is speech only: this module
//! cannot produce a banking permit, connector proof or business operation.
use super::{identity::VerifiedSession, passkey::DeliverySink};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{sync::oneshot, time::Instant};
use webauthn_rs::prelude::*;

#[derive(Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub business_ids: Vec<String>,
    pub agent_ids: Vec<String>,
    pub subjects: Vec<String>,
    pub rp_id: String,
    pub origin: String,
    pub timeout_seconds: f64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            business_ids: vec![],
            agent_ids: vec![],
            subjects: vec![],
            rp_id: "docs.odion.ai".into(),
            origin: "https://docs.odion.ai".into(),
            timeout_seconds: 90.0,
        }
    }
}
impl Config {
    pub fn validate(&self) -> Result<(), String> {
        let u = Url::parse(&self.origin).map_err(|_| "invalid trusted demo origin")?;
        let domain = regex::Regex::new(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}$")
            .map_err(|_| "demo origin validation unavailable")?;
        if ![
            format!("https://{}", self.rp_id),
            format!("https://{}/", self.rp_id),
        ]
        .contains(&self.origin)
            || u.scheme() != "https"
            || u.host_str() != Some(&self.rp_id)
            || u.port().is_some()
            || !u.username().is_empty()
            || u.password().is_some()
            || !matches!(u.path(), "" | "/")
            || u.query().is_some()
            || u.fragment().is_some()
            || !domain.is_match(&self.rp_id)
            || !self.timeout_seconds.is_finite()
            || !(1.0..=120.0).contains(&self.timeout_seconds)
        {
            return Err("invalid trusted demo origin or timeout".into());
        }
        Ok(())
    }
    pub fn enabled(&self, scope: &VerifiedSession, transport: &str) -> bool {
        fn canon(s: &str) -> String {
            s.trim().to_lowercase().replace('-', "")
        }
        let selected = |items: &[String], s: &str| {
            !s.trim().is_empty()
                && items
                    .iter()
                    .any(|i| !i.trim().is_empty() && canon(i) == canon(s))
        };
        transport != "sip"
            && scope.claims()["sip_caller_verified"] != true
            && selected(&self.business_ids, scope.business_id())
            && selected(&self.agent_ids, scope.agent_id())
            && selected(&self.subjects, scope.end_user_id())
    }
}
pub fn requested(text: &str) -> bool {
    let normalized = text
        .to_ascii_lowercase()
        .chars()
        .map(|c| {
            if c.is_ascii_lowercase() || c == ' ' {
                c
            } else {
                ' '
            }
        })
        .collect::<String>()
        .replace("pass key", "passkey");
    let normalized = normalized.split_whitespace().collect::<Vec<_>>().join(" ");
    regex::Regex::new(r"^(?:(?:please|can we|could we|can you|could you|let s|i want to|i would like to) )?(?:test (?:my |the |a )?passkey|run (?:the |a )?passkey (?:test|demo)|verify my demo passkey)(?: please| now)?$").is_ok_and(|r|r.is_match(&normalized))
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Status {
    Verified,
    Rejected,
    Cancelled,
    Expired,
    Interrupted,
    DeliveryFailed,
    Unavailable,
}
fn message(s: Status) -> String {
    match s{
    Status::Verified=>"Your passkey proof passed the simulated bank verification. This was a demo only. No bank transaction was authorized.",
    Status::Cancelled=>"The passkey test was cancelled. Nothing was approved, and we can keep talking.",
    Status::Expired=>"The passkey test expired. Nothing was approved. Ask to test your passkey again when you're ready.",
    _=>"The passkey test did not pass. Nothing was approved. Check the app setup, then request a fresh test."
}.into()
}
enum Verification {
    Register(PasskeyRegistration),
    Authenticate(PasskeyAuthentication),
}
struct Pending {
    auth: String,
    correlation: String,
    run: u64,
    context: u64,
    expires: Instant,
    consumed: bool,
    verification: Option<Verification>,
    finish: Option<oneshot::Sender<Status>>,
}
struct Inner {
    context_id: Option<String>,
    context: u64,
    user: Uuid,
    credential: Option<Passkey>,
    pending: Option<Pending>,
    running: bool,
    run: u64,
    requests: usize,
    closed: bool,
}
#[derive(Clone)]
pub struct Demo {
    inner: Arc<Mutex<Inner>>,
    api: Arc<Webauthn>,
    deliver: DeliverySink,
    timeout: Duration,
}
impl Demo {
    pub fn new(c: &Config, deliver: DeliverySink) -> Result<Self, String> {
        c.validate()?;
        let origin = Url::parse(&c.origin).map_err(|_| "invalid demo origin")?;
        let timeout = Duration::from_secs_f64(c.timeout_seconds);
        let api = WebauthnBuilder::new(&c.rp_id, &origin)
            .map_err(|_| "invalid demo configuration")?
            .rp_name("Attentive simulated bank")
            .timeout(timeout)
            .allow_subdomains(false)
            .allow_any_port(false)
            .build()
            .map_err(|_| "demo verifier unavailable")?;
        Ok(Self {
            api: Arc::new(api),
            inner: Arc::new(Mutex::new(Inner {
                context_id: None,
                context: 0,
                user: Uuid::new_v4(),
                credential: None,
                pending: None,
                running: false,
                run: 0,
                requests: 0,
                closed: false,
            })),
            deliver,
            timeout,
        })
    }
    async fn send(&self, packet: Value) -> Result<(), String> {
        let payload = json!({"type":super::passkey::TOPIC,"payload":packet});
        tokio::time::timeout(Duration::from_secs(3), (self.deliver)(payload))
            .await
            .map_err(|_| "private demo delivery timed out".to_string())?
    }
    pub fn busy(&self) -> bool {
        self.inner.lock().map(|i| i.running).unwrap_or(false)
    }
    pub fn cancel(&self) {
        let run = self.inner.lock().map(|i| i.run).unwrap_or(0);
        drop(RunGuard {
            demo: self.clone(),
            run,
        });
    }
    pub fn close(&self) {
        if let Ok(mut i) = self.inner.lock() {
            i.closed = true;
            if let Some(mut p) = i.pending.take() {
                if let Some(tx) = p.finish.take() {
                    let _ = tx.send(Status::Interrupted);
                }
            }
            i.credential = None;
            i.context_id = None;
        }
    }
    fn context_matches(&self, run: u64, context: u64) -> bool {
        self.inner
            .lock()
            .is_ok_and(|i| !i.closed && i.running && i.run == run && i.context == context)
    }
    pub async fn run(&self, progress: DeliverySink) -> String {
        let (run, context, register) = {
            let Ok(mut i) = self.inner.lock() else {
                return message(Status::Unavailable);
            };
            if i.closed || i.context_id.is_none() {
                return "The passkey demo is not available in this app. Check the app and domain setup. No test was approved.".into();
            }
            if i.running {
                return "A passkey test is already waiting. Complete or cancel the current system sheet first.".into();
            }
            i.running = true;
            i.run += 1;
            (i.run, i.context, i.credential.is_none())
        };
        let _guard = RunGuard {
            demo: self.clone(),
            run,
        };
        if register {
            if progress(json!("This is a simulated bank test, with no money movement. First, create a demo passkey in Apple's sheet.")).await.is_err(){return message(Status::Interrupted);}
            if !self.context_matches(run, context) {
                return message(Status::Interrupted);
            }
            let status = self.ceremony(run, true).await;
            if status != Status::Verified {
                return message(status);
            }
        }
        if progress(json!(
            "Now use your passkey to verify this simulated test in Apple's sheet."
        ))
        .await
        .is_err()
        {
            return message(Status::Interrupted);
        }
        if !self.context_matches(run, context) {
            return message(Status::Interrupted);
        }
        message(self.ceremony(run, false).await)
    }
    async fn ceremony(&self, run: u64, register: bool) -> Status {
        let setup = (|| -> Result<_, String> {
            let mut i = self.inner.lock().map_err(|_| "demo closed")?;
            if i.closed || !i.running || i.run != run || i.requests >= 6 {
                return Err("demo unavailable".into());
            }
            let (mut options, verification) = if register {
                let name = format!("demo-{}", &Uuid::new_v4().simple().to_string()[..10]);
                let (request, state) = self
                    .api
                    .start_passkey_registration(
                        i.user,
                        &name,
                        "Attentive demo — no bank account",
                        None,
                    )
                    .map_err(|_| "demo registration unavailable")?;
                let mut value = serde_json::to_value(request)
                    .map_err(|_| "demo options unavailable")?["publicKey"]
                    .clone();
                // The high-level server still verifies the complete ceremony.
                // Reject any non-ES256 credential after verification as well.
                value["pubKeyCredParams"] = json!([{"type":"public-key","alg":-7}]);
                value["authenticatorSelection"] = json!({"authenticatorAttachment":"platform","residentKey":"required","requireResidentKey":true,"userVerification":"required"});
                value["attestation"] = json!("none");
                (value, Verification::Register(state))
            } else {
                let credential = i.credential.as_ref().ok_or("no demo credential")?;
                let (request, state) = self
                    .api
                    .start_passkey_authentication(std::slice::from_ref(credential))
                    .map_err(|_| "demo authentication unavailable")?;
                (
                    serde_json::to_value(request).map_err(|_| "demo options unavailable")?
                        ["publicKey"]
                        .clone(),
                    Verification::Authenticate(state),
                )
            };
            options["timeout"] = json!(self.timeout.as_millis() as u64);
            let auth = Uuid::new_v4().to_string();
            let correlation = Uuid::new_v4().to_string();
            let (tx, rx) = oneshot::channel();
            i.requests += 1;
            i.pending = Some(Pending {
                auth: auth.clone(),
                correlation: correlation.clone(),
                run,
                context: i.context,
                expires: Instant::now() + self.timeout,
                consumed: false,
                verification: Some(verification),
                finish: Some(tx),
            });
            let expires = chrono::Utc::now()
                + chrono::Duration::from_std(self.timeout)
                    .map_err(|_| "demo deadline unavailable")?;
            let packet = json!({"type":"fido2_ceremony","authenticationId":auth,"correlationId":correlation,"action":1,"feature":"AttentiveDemoOnly","mode":if register{"register"}else{"authenticate"},"expiresAt":expires.to_rfc3339_opts(chrono::SecondsFormat::Micros,false),"options":options});
            Ok((packet, rx, auth, correlation))
        })();
        let Ok((packet, rx, auth, correlation)) = setup else {
            return Status::Unavailable;
        };
        if self.send(packet).await.is_err() {
            self.finish(&auth, Status::DeliveryFailed);
            return Status::DeliveryFailed;
        }
        let outcome = match tokio::time::timeout(self.timeout, rx).await {
            Ok(Ok(s)) => s,
            _ => {
                let _=self.send(json!({"type":"fido2_abort","authenticationId":auth,"correlationId":correlation})).await;
                Status::Expired
            }
        };
        if let Ok(mut i) = self.inner.lock() {
            if i.pending.as_ref().is_some_and(|p| p.auth == auth) {
                i.pending = None;
            }
        }
        outcome
    }
    fn finish(&self, auth: &str, status: Status) {
        if let Ok(mut i) = self.inner.lock() {
            if let Some(p) = i.pending.as_mut().filter(|p| p.auth == auth) {
                p.consumed = true;
                if let Some(tx) = p.finish.take() {
                    let _ = tx.send(status);
                }
            }
        }
    }
    /// Parse and consume ownership synchronously; only one proof per ceremony
    /// can schedule cryptographic work. No credential/proof is sent to a model.
    pub fn receive(&self, raw: &[u8]) {
        if raw.len() > 14000 {
            return;
        }
        let Ok(value) = serde_json::from_slice::<Value>(raw) else {
            return;
        };
        if !value.is_object() || value.get("version").is_some_and(|v| v.as_u64() != Some(1)) {
            return;
        }
        let Ok(mut i) = self.inner.lock() else {
            return;
        };
        if i.closed {
            return;
        }
        if value["type"] == "session_context" {
            let Some(context) = value["correlationId"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 256)
            else {
                return;
            };
            if i.context_id.as_deref().is_some_and(|old| old != context) {
                if let Some(mut p) = i.pending.take() {
                    if let Some(tx) = p.finish.take() {
                        let _ = tx.send(Status::Interrupted);
                    }
                }
                i.credential = None;
                i.user = Uuid::new_v4();
                i.context += 1;
            }
            i.context_id = Some(context.into());
            return;
        }
        let stored = i.credential.clone();
        let user = i.user;
        let Some(p) = i.pending.as_mut().filter(|p| {
            !p.consumed
                && value["authenticationId"] == p.auth
                && value["correlationId"] == p.correlation
        }) else {
            return;
        };
        match value["type"].as_str() {
            Some("fido2_cancelled" | "fido2_failed") => {
                p.consumed = true;
                let status = if value["type"] == "fido2_cancelled" {
                    Status::Cancelled
                } else {
                    Status::Rejected
                };
                if let Some(tx) = p.finish.take() {
                    let _ = tx.send(status);
                }
            }
            Some("fido2_proof") => {
                p.consumed = true;
                let (auth, correlation, context, expires) =
                    (p.auth.clone(), p.correlation.clone(), p.context, p.expires);
                let verification = p.verification.take();
                let demo = self.clone();
                let api = self.api.clone();
                drop(i);
                tokio::spawn(async move {
                    let candidate = if Instant::now() < expires {
                        tokio::task::spawn_blocking(move || {
                            verify(&api, verification, &value["proof"], stored, user)
                        })
                        .await
                        .ok()
                        .flatten()
                    } else {
                        None
                    };
                    let accepted = if let Ok(mut i) = demo.inner.lock() {
                        if !i.closed
                            && i.context == context
                            && Instant::now() < expires
                            && i.pending
                                .as_ref()
                                .is_some_and(|p| p.auth == auth && p.correlation == correlation)
                        {
                            if let Some(candidate) = candidate {
                                i.credential = Some(candidate);
                                true
                            } else {
                                false
                            }
                        } else {
                            false
                        }
                    } else {
                        false
                    };
                    let delivered=demo.send(json!({"type":"fido2_result","authenticationId":auth,"correlationId":correlation,"accepted":accepted,"outcome":if accepted{"demo_verified"}else{"demo_rejected"}})).await.is_ok();
                    demo.finish(
                        &auth,
                        if !delivered {
                            Status::DeliveryFailed
                        } else if accepted {
                            Status::Verified
                        } else {
                            Status::Rejected
                        },
                    );
                });
            }
            _ => {}
        }
    }
}
fn verify(
    api: &Webauthn,
    verification: Option<Verification>,
    proof: &Value,
    stored: Option<Passkey>,
    user: Uuid,
) -> Option<Passkey> {
    match verification? {
        Verification::Register(state) => {
            let response =
                serde_json::from_value::<RegisterPublicKeyCredential>(proof.clone()).ok()?;
            let key = api.finish_passkey_registration(&response, &state).ok()?;
            (*key.cred_algorithm() == COSEAlgorithm::ES256).then_some(key)
        }
        Verification::Authenticate(state) => {
            let mut stored = stored?;
            let credential_id: &[u8] = stored.cred_id().as_ref();
            let id = URL_SAFE_NO_PAD.encode(credential_id);
            if proof["id"] != id || proof["rawId"] != id {
                return None;
            }
            if proof["response"].get("userHandle").is_some_and(|v| {
                !v.is_null() && *v != json!(URL_SAFE_NO_PAD.encode(user.as_bytes()))
            }) {
                return None;
            }
            let response = serde_json::from_value::<PublicKeyCredential>(proof.clone()).ok()?;
            let result = api.finish_passkey_authentication(&response, &state).ok()?;
            if !result.user_verified() || stored.update_credential(&result).is_none() {
                return None;
            }
            Some(stored)
        }
    }
}
struct RunGuard {
    demo: Demo,
    run: u64,
}
impl Drop for RunGuard {
    fn drop(&mut self) {
        let packet = if let Ok(mut i) = self.demo.inner.lock() {
            if i.run != self.run {
                return;
            }
            i.running = false;
            i.pending.take().filter(|p|p.run==self.run).map(|mut p|{if let Some(tx)=p.finish.take(){let _=tx.send(Status::Interrupted);}json!({"type":"fido2_abort","authenticationId":p.auth,"correlationId":p.correlation})})
        } else {
            None
        };
        if let Some(packet) = packet {
            let demo = self.demo.clone();
            tokio::spawn(async move {
                let _ = demo.send(packet).await;
            });
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use p256::ecdsa::{signature::Signer, Signature, SigningKey};
    use serde_cbor_2::Value as Cbor;
    use sha2::{Digest, Sha256};
    use std::collections::BTreeMap;
    use tokio::sync::mpsc;
    pub(crate) struct Device {
        key: SigningKey,
        id: Vec<u8>,
        user: Option<String>,
        pub counter: u32,
    }
    impl Device {
        pub fn new() -> Self {
            Self {
                key: SigningKey::from_bytes((&[7u8; 32]).into()).unwrap(),
                id: vec![9; 32],
                user: None,
                counter: 0,
            }
        }
        pub fn proof(&mut self, packet: &Value, uv: bool, origin: &str) -> Value {
            let options = &packet["options"];
            let register = packet["mode"] == "register";
            let client=serde_json::to_vec(&json!({"type":if register{"webauthn.create"}else{"webauthn.get"},"origin":origin,"challenge":options["challenge"]})).unwrap();
            let mut auth = Sha256::digest(b"docs.odion.ai").to_vec();
            auth.push(1 | if uv { 4 } else { 0 } | if register { 64 } else { 0 });
            let response = if register {
                self.user = options["user"]["id"].as_str().map(str::to_owned);
                auth.extend(0u32.to_be_bytes());
                auth.extend([0u8; 16]);
                auth.extend((self.id.len() as u16).to_be_bytes());
                auth.extend(&self.id);
                let point = self.key.verifying_key().to_encoded_point(false);
                let cose: BTreeMap<Cbor, Cbor> = [
                    (Cbor::Integer(1), Cbor::Integer(2)),
                    (Cbor::Integer(3), Cbor::Integer(-7)),
                    (Cbor::Integer(-1), Cbor::Integer(1)),
                    (Cbor::Integer(-2), Cbor::Bytes(point.x().unwrap().to_vec())),
                    (Cbor::Integer(-3), Cbor::Bytes(point.y().unwrap().to_vec())),
                ]
                .into_iter()
                .collect();
                auth.extend(serde_cbor_2::to_vec(&cose).unwrap());
                let attestation: BTreeMap<Cbor, Cbor> = [
                    (Cbor::Text("fmt".into()), Cbor::Text("none".into())),
                    (Cbor::Text("attStmt".into()), Cbor::Map(BTreeMap::new())),
                    (Cbor::Text("authData".into()), Cbor::Bytes(auth)),
                ]
                .into_iter()
                .collect();
                json!({"clientDataJSON":URL_SAFE_NO_PAD.encode(&client),"attestationObject":URL_SAFE_NO_PAD.encode(serde_cbor_2::to_vec(&attestation).unwrap())})
            } else {
                self.counter += 1;
                auth.extend(self.counter.to_be_bytes());
                let mut signed = auth.clone();
                signed.extend(Sha256::digest(&client));
                let sig: Signature = self.key.sign(&signed);
                json!({"clientDataJSON":URL_SAFE_NO_PAD.encode(&client),"authenticatorData":URL_SAFE_NO_PAD.encode(auth),"signature":URL_SAFE_NO_PAD.encode(sig.to_der().as_bytes()),"userHandle":self.user})
            };
            json!({"id":URL_SAFE_NO_PAD.encode(&self.id),"rawId":URL_SAFE_NO_PAD.encode(&self.id),"type":"public-key","response":response,"clientExtensionResults":{}})
        }
    }
    fn setup() -> (Demo, mpsc::Receiver<Value>, Arc<Mutex<Vec<Value>>>) {
        let (tx, rx) = mpsc::channel(32);
        let deliver: DeliverySink = Arc::new(move |body| {
            let tx = tx.clone();
            Box::pin(async move {
                assert_eq!(body["type"], super::super::passkey::TOPIC);
                tx.send(body["payload"].clone())
                    .await
                    .map_err(|_| "closed".into())
            })
        });
        let demo = Demo::new(
            &Config {
                timeout_seconds: 2.0,
                ..Default::default()
            },
            deliver,
        )
        .unwrap();
        demo.receive(
            &serde_json::to_vec(&json!({"type":"session_context","correlationId":"context"}))
                .unwrap(),
        );
        (demo, rx, Arc::new(Mutex::new(vec![])))
    }
    fn run(demo: &Demo, progress: Arc<Mutex<Vec<Value>>>) -> tokio::task::JoinHandle<String> {
        let d = demo.clone();
        tokio::spawn(async move {
            let progress: DeliverySink = Arc::new(move |v| {
                progress.lock().unwrap().push(v);
                Box::pin(async { Ok(()) })
            });
            d.run(progress).await
        })
    }
    async fn packet(rx: &mut mpsc::Receiver<Value>) -> Value {
        tokio::time::timeout(Duration::from_secs(5), rx.recv())
            .await
            .unwrap()
            .unwrap()
    }
    fn prove(demo: &Demo, packet: &Value, proof: Value) -> Value {
        let frame = json!({"type":"fido2_proof","authenticationId":packet["authenticationId"],"correlationId":packet["correlationId"],"proof":proof});
        demo.receive(&serde_json::to_vec(&frame).unwrap());
        frame
    }
    #[tokio::test]
    async fn native_registration_and_assertion_verify_real_es256_signatures() {
        let (demo, mut rx, progress) = setup();
        let task = run(&demo, progress.clone());
        let mut device = Device::new();
        let create = packet(&mut rx).await;
        assert_eq!(create["mode"], "register");
        assert_eq!(
            create["options"]["pubKeyCredParams"],
            json!([{"type":"public-key","alg":-7}])
        );
        prove(
            &demo,
            &create,
            device.proof(&create, true, "https://docs.odion.ai"),
        );
        assert_eq!(packet(&mut rx).await["accepted"], true);
        let auth = packet(&mut rx).await;
        assert_eq!(auth["mode"], "authenticate");
        let frame = prove(
            &demo,
            &auth,
            device.proof(&auth, true, "https://docs.odion.ai"),
        );
        assert_eq!(packet(&mut rx).await["accepted"], true);
        assert!(task
            .await
            .unwrap()
            .contains("No bank transaction was authorized"));
        assert_eq!(progress.lock().unwrap().len(), 2);
        demo.receive(&serde_json::to_vec(&frame).unwrap());
        assert!(rx.try_recv().is_err());
        demo.close();
    }
    #[tokio::test]
    async fn bad_origin_uv_and_challenge_are_rejected_and_consumed() {
        for mode in 0..3 {
            let (demo, mut rx, progress) = setup();
            let task = run(&demo, progress);
            let mut device = Device::new();
            let create = packet(&mut rx).await;
            let mut wrong = create.clone();
            if mode == 2 {
                wrong["options"]["challenge"] = json!(URL_SAFE_NO_PAD.encode(b"wrong"));
            }
            let proof = device.proof(
                &wrong,
                mode != 1,
                if mode == 0 {
                    "https://evil.example"
                } else {
                    "https://docs.odion.ai"
                },
            );
            let replay = prove(&demo, &create, proof);
            assert_eq!(packet(&mut rx).await["accepted"], false);
            assert!(task.await.unwrap().contains("did not pass"));
            demo.receive(&serde_json::to_vec(&replay).unwrap());
            assert!(rx.try_recv().is_err());
            assert!(demo.inner.lock().unwrap().credential.is_none());
        }
    }
    #[tokio::test]
    async fn context_reset_discards_credentials_and_old_ids_cannot_complete_new_run() {
        let (demo, mut rx, progress) = setup();
        let task = run(&demo, progress.clone());
        let mut device = Device::new();
        let create = packet(&mut rx).await;
        prove(
            &demo,
            &create,
            device.proof(&create, true, "https://docs.odion.ai"),
        );
        assert_eq!(packet(&mut rx).await["accepted"], true);
        let auth = packet(&mut rx).await;
        demo.receive(
            &serde_json::to_vec(
                &json!({"type":"session_context","correlationId":"changed-context"}),
            )
            .unwrap(),
        );
        assert!(task.await.unwrap().contains("did not pass"));
        assert!(demo.inner.lock().unwrap().credential.is_none());
        let task = run(&demo, progress);
        let newer = packet(&mut rx).await;
        assert_eq!(newer["mode"], "register");
        prove(
            &demo,
            &auth,
            device.proof(&auth, true, "https://docs.odion.ai"),
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
        assert!(!task.is_finished());
        assert!(rx.try_recv().is_err());
        demo.receive(&serde_json::to_vec(&json!({"type":"fido2_cancelled","authenticationId":newer["authenticationId"],"correlationId":newer["correlationId"]})).unwrap());
        assert!(task.await.unwrap().contains("cancelled"));
    }
    #[test]
    fn intent_and_messages_match_unchanged_python() {
        let f: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for c in f["demo_requests"].as_array().unwrap() {
            assert_eq!(requested(c["text"].as_str().unwrap()), c["expected"], "{c}");
        }
        for (label, status) in [
            ("verified", Status::Verified),
            ("cancelled", Status::Cancelled),
            ("expired", Status::Expired),
            ("interrupted", Status::Interrupted),
            ("delivery_failed", Status::DeliveryFailed),
        ] {
            assert_eq!(message(status), f["demo_messages"][label]);
        }
    }
    #[test]
    fn scope_defaults_and_trusted_origins_fail_closed() {
        let scope = super::super::identity::test_session(json!({}));
        let mut c = Config::default();
        assert!(!c.enabled(&scope, "browser"));
        c.business_ids = vec![scope.business_id().to_uppercase()];
        c.agent_ids = vec![scope.agent_id().into()];
        c.subjects = vec![scope.end_user_id().into()];
        assert!(c.enabled(&scope, "browser"));
        assert!(!c.enabled(&scope, "sip"));
        for origin in [
            "http://docs.odion.ai",
            "https://docs.odion.ai:443",
            "https://evil.example",
            "https://docs.odion.ai/path",
            "https://docs.odion.ai#x",
        ] {
            c.origin = origin.into();
            assert!(c.validate().is_err(), "{origin}");
        }
    }
    #[tokio::test]
    async fn fresh_signed_assertion_with_reused_counter_is_rejected() {
        let (demo, mut rx, progress) = setup();
        let task = run(&demo, progress.clone());
        let mut device = Device::new();
        let create = packet(&mut rx).await;
        prove(
            &demo,
            &create,
            device.proof(&create, true, "https://docs.odion.ai"),
        );
        assert_eq!(packet(&mut rx).await["accepted"], true);
        let auth = packet(&mut rx).await;
        prove(
            &demo,
            &auth,
            device.proof(&auth, true, "https://docs.odion.ai"),
        );
        assert_eq!(packet(&mut rx).await["accepted"], true);
        task.await.unwrap();
        let task = run(&demo, progress);
        let auth = packet(&mut rx).await;
        device.counter = 0;
        prove(
            &demo,
            &auth,
            device.proof(&auth, true, "https://docs.odion.ai"),
        );
        assert_eq!(packet(&mut rx).await["accepted"], false);
        assert!(task.await.unwrap().contains("did not pass"));
    }
    #[tokio::test(start_paused = true)]
    async fn timeout_and_cancellation_abort_only_the_owned_prompt() {
        let (demo, mut rx, progress) = setup();
        let task = run(&demo, progress.clone());
        let create = packet(&mut rx).await;
        tokio::time::advance(Duration::from_secs(3)).await;
        assert!(task.await.unwrap().contains("expired"));
        let abort = packet(&mut rx).await;
        assert_eq!(abort["type"], "fido2_abort");
        assert_eq!(abort["authenticationId"], create["authenticationId"]);
        let task = run(&demo, progress);
        let create = packet(&mut rx).await;
        demo.cancel();
        task.abort();
        let _ = task.await;
        let abort = packet(&mut rx).await;
        assert_eq!(abort["type"], "fido2_abort");
        assert_eq!(abort["authenticationId"], create["authenticationId"]);
        assert!(!demo.busy());
    }
}
