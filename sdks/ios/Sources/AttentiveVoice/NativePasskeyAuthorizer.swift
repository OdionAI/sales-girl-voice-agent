#if os(iOS)
import AuthenticationServices
import UIKit

@MainActor
final class NativePasskeyAuthorizer: NSObject, PasskeyAuthorizing, ASAuthorizationControllerDelegate, ASAuthorizationControllerPresentationContextProviding {
    private let anchor: () -> UIWindow?
    private var window: UIWindow?
    private var controller: ASAuthorizationController?
    private var continuation: CheckedContinuation<PasskeyProof, Error>?
    private var mode: PasskeyRequest.Mode?
    private var backgroundObserver: NSObjectProtocol?
    private let diagnostics: PasskeyDiagnostics?

    init(diagnostics: PasskeyDiagnostics? = nil, anchor: @escaping () -> UIWindow?) {
        self.diagnostics = diagnostics
        self.anchor = anchor
        super.init()
        backgroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard UIApplication.shared.applicationState == .background else { return }
                self?.cancel()
            }
        }
    }

    deinit {
        if let backgroundObserver { NotificationCenter.default.removeObserver(backgroundObserver) }
    }

    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof {
        diagnostics?.record(.presentation, .started)
        guard controller == nil else { throw PasskeyFailure.busy }
        guard let window = anchor(), window.windowScene?.activationState == .foregroundActive,
              !window.isHidden else {
            diagnostics?.record(.presentation, .failed, reason: .presentationUnavailable)
            throw PasskeyFailure.presentationUnavailable
        }
        let authorization = try authorizationRequest(for: request)
        self.window = window
        mode = request.mode
        let controller = ASAuthorizationController(authorizationRequests: [authorization])
        self.controller = controller
        controller.delegate = self
        controller.presentationContextProvider = self
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            diagnostics?.record(.presentation, .requested)
            controller.performRequests()
        }
    }

    // Internal typed mapping is separately testable without invoking credential UI.
    func authorizationRequest(for request: PasskeyRequest) throws -> ASAuthorizationRequest {
        let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: request.relyingParty)
        let authorization: ASAuthorizationRequest
        switch request.mode {
        case .authenticate:
            let assertion = provider.createCredentialAssertionRequest(challenge: request.challenge)
            assertion.userVerificationPreference = .init(rawValue: request.userVerification.rawValue)
            assertion.allowedCredentials = request.credentialIDs.map { .init(credentialID: $0) }
            authorization = assertion
        case .register:
            guard let userID = request.userID, let name = request.userName else { throw PasskeyFailure.invalidRequest }
            let registration = provider.createCredentialRegistrationRequest(challenge: request.challenge, name: name, userID: userID)
            registration.displayName = request.displayName
            registration.userVerificationPreference = .init(rawValue: request.userVerification.rawValue)
            registration.attestationPreference = .none
            if !request.credentialIDs.isEmpty {
                if #available(iOS 17.4, *) {
                    registration.excludedCredentials = request.credentialIDs.map { .init(credentialID: $0) }
                } else { throw PasskeyFailure.unsupportedOptions }
            }
            authorization = registration
        }
        return authorization
    }

    func cancel() {
        let controller = controller
        complete(.failure(PasskeyFailure.cancelled))
        controller?.cancel()
    }

    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        // Captured from the presenting view's scene, never a global "first key window".
        window ?? UIWindow()
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithAuthorization authorization: ASAuthorization) {
        guard self.controller === controller else { return }
        diagnostics?.record(.presentation, .succeeded)
        diagnostics?.record(.proof, .started)
        guard let window, anchor() === window, window.windowScene?.activationState == .foregroundActive else {
            complete(.failure(PasskeyFailure.presentationUnavailable)); return
        }
        if mode == .authenticate, let credential = authorization.credential as? ASAuthorizationPlatformPublicKeyCredentialAssertion,
           !credential.credentialID.isEmpty, !credential.rawClientDataJSON.isEmpty,
           !credential.rawAuthenticatorData.isEmpty, !credential.signature.isEmpty {
            complete(.success(.assertion(id: credential.credentialID, clientData: credential.rawClientDataJSON,
                                         authenticatorData: credential.rawAuthenticatorData, signature: credential.signature, userID: credential.userID)))
        } else if mode == .register, let credential = authorization.credential as? ASAuthorizationPlatformPublicKeyCredentialRegistration,
                  !credential.credentialID.isEmpty, !credential.rawClientDataJSON.isEmpty,
                  let attestation = credential.rawAttestationObject, !attestation.isEmpty {
            complete(.success(.registration(id: credential.credentialID, clientData: credential.rawClientDataJSON, attestation: attestation)))
        } else { complete(.failure(PasskeyFailure.invalidCredential)) }
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: Error) {
        guard self.controller === controller else { return }
        let cancelled = (error as? ASAuthorizationError)?.code == .canceled
        let failure: PasskeyFailure = cancelled ? .cancelled : .authorizationFailed
        let system = PasskeySystemError(error as NSError)
        diagnostics?.record(.presentation, cancelled ? .cancelled : .failed, reason: failure, systemError: system)
        complete(.failure(PasskeyNativeError(failure: failure, system: system)))
    }

    private func complete(_ result: Result<PasskeyProof, Error>) {
        let continuation = continuation
        self.continuation = nil
        controller?.delegate = nil; controller?.presentationContextProvider = nil
        controller = nil; window = nil; mode = nil
        continuation?.resume(with: result)
    }
}
#endif
