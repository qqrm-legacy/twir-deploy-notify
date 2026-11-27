use std::fs;

fn workflow(name: &str) -> String {
    fs::read_to_string(format!(".github/workflows/{name}"))
        .unwrap_or_else(|error| panic!("failed to read {name}: {error}"))
}

fn assert_safe_marker_restore(workflow: &str) {
    let checkout = workflow
        .find("- name: Checkout repository")
        .expect("the trusted marker helper must be checked out before it runs");
    let restore = workflow
        .find("python3 -I .github/scripts/restore_marker.py")
        .expect("marker recovery must use the shared isolated data-only helper");
    assert!(checkout < restore);
    assert!(workflow.contains("DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}"));
    assert!(
        !workflow.contains("gh run download"),
        "workflows must not extract an entire artifact into the checkout"
    );
}

#[test]
fn production_workflow_preserves_the_marker_across_noop_runs() {
    let production = workflow("prod.yml");
    assert_safe_marker_restore(&production);

    for required in [
        "python3 -I .github/scripts/restore_marker.py prod",
        "last_sent=$(cat last_sent.txt 2>/dev/null || echo \"\")",
        "- name: Preserve last_sent marker when delivery is not needed",
        "if: steps.decide.outputs.should_send == 'false'",
        "- name: Upload preserved last_sent marker",
        "name: last-sent-prod",
        "path: last_sent.txt",
        "if-no-files-found: error",
    ] {
        assert!(
            production.contains(required),
            "production workflow is missing required delivery-state contract: {required}"
        );
    }
}

#[test]
fn delivery_workflow_writes_the_marker_only_after_a_delivery() {
    let delivery = workflow("common-delivery.yml");
    assert_safe_marker_restore(&delivery);
    assert!(
        !delivery.contains("if: always()\n        uses: actions/upload-artifact@v7\n        with:\n          name: last-sent-"),
        "the last_sent artifact must be uploaded only by successful runs"
    );

    for required in [
        "python3 -I .github/scripts/restore_marker.py \"$ARTIFACT_SCOPE\"",
        "- name: Save last_sent marker",
        "steps.prepare.outputs.send == 'true' && (inputs.send_main == true || inputs.send_dev == true)",
        "printf '%s\\n' \"$LATEST_POST\" > last_sent.txt",
        "- name: Upload last_sent artifact",
        "name: last-sent-${{ env.ARTIFACT_SCOPE }}",
        "path: last_sent.txt",
    ] {
        assert!(
            delivery.contains(required),
            "common delivery workflow is missing required delivery-state contract: {required}"
        );
    }
}

#[test]
fn production_send_is_guarded_by_the_channel_pin_check() {
    let cli = fs::read_to_string("src/cli.rs")
        .unwrap_or_else(|error| panic!("failed to read src/cli.rs: {error}"));

    let guard_position = cli
        .find("pin_guard::enforce_pin_guard")
        .expect("production send must be preceded by the Telegram pin guard");
    let send_position = cli
        .find("Sending posts to production Telegram chat")
        .expect("production send must exist");
    assert!(
        guard_position < send_position,
        "the pin guard must run before the production Telegram send"
    );
}
