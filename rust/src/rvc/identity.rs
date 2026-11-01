//! Native verification of the dashboard's short-lived, one-use call bootstrap.
//! A room name or editable participant metadata is never banking identity.
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hmac::{Hmac, Mac};
use serde_json::Value;
use sha2::Sha256;
use std::{collections::HashMap, sync::Mutex};

#[derive(Clone)]
pub struct VerifiedSession {
    claims: Value,
    business_id: String,
    agent_id: String,
    end_user_id: String,
}
impl VerifiedSession {
    /// Only the SDK/pinned-trunk verifier can call this constructor; raw
    /// participant metadata cannot construct a verified session externally.
    pub(super) fn from_pinned_sip(claims: Value) -> Result<Self, String> {
        let field = |name: &str| {
            claims[name]
                .as_str()
                .filter(|v| !v.trim().is_empty())
                .map(|v| v.trim().to_string())
                .ok_or("invalid pinned SIP identity".to_string())
        };
        let business_id = field("business_id")?;
        let agent_id = field("config_agent_id")?;
        let end_user_id = field("end_user_id")?;
        if claims["entry_surface"] != "aicc_inbound" || claims["sip_caller_verified"] != true {
            return Err("invalid pinned SIP identity".into());
        }
        Ok(Self {
            business_id,
            agent_id,
            end_user_id,
            claims,
        })
    }
    pub fn claims(&self) -> &Value {
        &self.claims
    }
    pub fn business_id(&self) -> &str {
        &self.business_id
    }
    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }
    pub fn end_user_id(&self) -> &str {
        &self.end_user_id
    }
    pub fn caller(&self) -> &str {
        self.claims["participant_identity"].as_str().unwrap_or("")
    }
    pub fn room(&self) -> &str {
        self.claims["room_name"].as_str().unwrap_or("")
    }
}
#[derive(Default)]
pub struct Verifier {
    used: Mutex<HashMap<String, f64>>,
}
fn decode(value: &str) -> Result<Vec<u8>, String> {
    URL_SAFE_NO_PAD
        .decode(value.trim_end_matches('='))
        .map_err(|_| "Invalid call bootstrap.".into())
}
impl Verifier {
    pub fn verify(
        &self,
        token: &str,
        service_token: &str,
        now: f64,
    ) -> Result<VerifiedSession, String> {
        let invalid = || "Invalid or expired call bootstrap. Start a new call.".to_string();
        if service_token.is_empty() || token.len() > 16384 || !now.is_finite() {
            return Err(invalid());
        }
        let (encoded, signature) = token.split_once('.').ok_or_else(invalid)?;
        let mut key =
            Hmac::<Sha256>::new_from_slice(service_token.as_bytes()).map_err(|_| invalid())?;
        key.update(b"odion-rvc-session-v1");
        let key = key.finalize().into_bytes();
        let mut mac = Hmac::<Sha256>::new_from_slice(&key).map_err(|_| invalid())?;
        mac.update(encoded.as_bytes());
        mac.verify_slice(&decode(signature)?)
            .map_err(|_| invalid())?;
        let claims: Value = serde_json::from_slice(&decode(encoded)?).map_err(|_| invalid())?;
        let expiry = claims["exp"]
            .as_f64()
            .or_else(|| claims["exp"].as_str()?.parse().ok())
            .ok_or_else(invalid)?;
        let nonce = claims["nonce"].as_str().ok_or_else(invalid)?;
        if claims["aud"] != "odion-rvc-tools"
            || !(now < expiry && expiry <= now + 180.0)
            || !(16..=128).contains(&nonce.chars().count())
        {
            return Err(invalid());
        }
        for name in ["business_id", "config_agent_id", "end_user_id"] {
            if claims[name].as_str().is_none_or(|s| s.trim().is_empty()) {
                return Err(invalid());
            }
        }
        let mut used = self.used.lock().map_err(|_| invalid())?;
        used.retain(|_, expiry| *expiry > now);
        if used.contains_key(nonce) {
            return Err(invalid());
        }
        used.insert(nonce.into(), expiry);
        Ok(VerifiedSession {
            business_id: claims["business_id"]
                .as_str()
                .ok_or_else(invalid)?
                .trim()
                .into(),
            agent_id: claims["config_agent_id"]
                .as_str()
                .ok_or_else(invalid)?
                .trim()
                .into(),
            end_user_id: claims["end_user_id"]
                .as_str()
                .ok_or_else(invalid)?
                .trim()
                .into(),
            claims,
        })
    }
    /// Validate dispatch scope before granting any tool-capable context.
    pub fn dispatch(
        &self,
        metadata: &str,
        room: &str,
        service_token: &str,
        now: f64,
    ) -> Result<VerifiedSession, String> {
        let data: Value = serde_json::from_str(metadata).map_err(|_| "Invalid call dispatch.")?;
        let session = self.verify(
            data["session_token"].as_str().unwrap_or(""),
            service_token,
            now,
        )?;
        if session.room() != room || session.caller().is_empty() {
            return Err("Call bootstrap does not match this room and caller.".into());
        }
        Ok(session)
    }
}
#[cfg(test)]
pub(crate) fn test_session(extra: Value) -> VerifiedSession {
    let mut claims = serde_json::json!({"aud":"odion-rvc-tools","exp":1100,"nonce":"native-banking-fixture-nonce-0001","business_id":"business-a","config_agent_id":"agent-a","end_user_id":"caller-a","room_name":"room-a","participant_identity":"caller"});
    if let Some(extra) = extra.as_object() {
        for (key, value) in extra {
            claims[key] = value.clone();
        }
    }
    let key = Hmac::<Sha256>::new_from_slice(b"local-parity-fixture-only").unwrap();
    let mut derived = key;
    derived.update(b"odion-rvc-session-v1");
    let mut mac = Hmac::<Sha256>::new_from_slice(&derived.finalize().into_bytes()).unwrap();
    let encoded = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&claims).unwrap());
    mac.update(encoded.as_bytes());
    let token = format!(
        "{}.{}",
        encoded,
        URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
    );
    Verifier::default()
        .verify(&token, "local-parity-fixture-only", 1000.0)
        .unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    const KEY: &str = "local-parity-fixture-only";
    fn sign(claims: &Value) -> String {
        let encoded = URL_SAFE_NO_PAD.encode(claims.to_string());
        let mut key = Hmac::<Sha256>::new_from_slice(KEY.as_bytes()).unwrap();
        key.update(b"odion-rvc-session-v1");
        let mut mac = Hmac::<Sha256>::new_from_slice(&key.finalize().into_bytes()).unwrap();
        mac.update(encoded.as_bytes());
        format!(
            "{encoded}.{}",
            URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
        )
    }
    fn claims() -> Value {
        serde_json::json!({"aud":"odion-rvc-tools","exp":1100,"nonce":"a-local-nonce-12345","business_id":"business-a","config_agent_id":"agent-a","end_user_id":"caller-a","room_name":"room-a","participant_identity":"caller"})
    }
    #[test]
    fn scoped_once_only_and_expiry_bounds() {
        let v = Verifier::default();
        let token = sign(&claims());
        let metadata = serde_json::json!({"session_token":token}).to_string();
        assert_eq!(
            v.dispatch(&metadata, "room-a", KEY, 1000.0)
                .unwrap()
                .business_id(),
            "business-a"
        );
        assert!(v.verify(&token, KEY, 1000.0).is_err());
        assert!(Verifier::default().verify(&token, KEY, 1100.0).is_err());
        assert!(Verifier::default().verify(&token, KEY, 900.0).is_err());
        assert!(Verifier::default()
            .dispatch(&metadata, "room-b", KEY, 1000.0)
            .is_err());
        assert!(Verifier::default()
            .verify(&token, "other-key", 1000.0)
            .is_err());
    }
    #[test]
    fn rejects_invalid_scope_audience_and_tampering() {
        for (name, value) in [
            ("business_id", serde_json::json!(" ")),
            ("end_user_id", Value::Null),
            ("config_agent_id", serde_json::json!(123)),
            ("aud", serde_json::json!("other")),
            ("nonce", serde_json::json!("short")),
        ] {
            let mut c = claims();
            c[name] = value;
            assert!(Verifier::default().verify(&sign(&c), KEY, 1000.0).is_err());
        }
        let token = sign(&claims());
        let tampered = token.replacen('a', "b", 1);
        assert!(Verifier::default().verify(&tampered, KEY, 1000.0).is_err());
    }
}
