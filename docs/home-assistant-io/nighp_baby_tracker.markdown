---
title: Baby Tracker (Nighp)
description: Instructions on how to integrate your Nighp Baby Tracker account into Home Assistant.
ha_categories:
  - health_wellbeing
ha_release: 2026.12
ha_iot_class: Cloud Polling
ha_codeowners:
  - '@wtadler'
ha_config_flow: true
ha_domain: nighp_baby_tracker
ha_platforms:
  - diagnostics
  - sensor
ha_integration_type: hub
ha_quality_scale: bronze
---

<!--- Source: the integration page template at source/_integrations/_integration_docs_template.markdown in home-assistant/home-assistant.io, last changed in commit 53439929be9cda80fbcb5d5c3bbb693dfc433769, with ohme.markdown (commit a3be2e6f89a8cf2b4229ca445cb670e1ddf560d7) as the recent cloud-polling example, read at docs repository commit 16bcbb9e127f12babd8e541a102746fb0b0147bb on October 27, 2026. To refresh, compare against the template's current commit and update this comment. Scope: the first core PR (config flow, coordinator, sensors, diagnostics). Before submitting, set ha_release to the release the PR targets and ha_quality_scale to the tier the integration reaches. -->

The **Baby Tracker (Nighp)** {% term integration %} connects your account for the Baby Tracker app by Nighp to Home Assistant. It shows each baby's last diaper, feed, and sleep as sensors, so you can see them on a dashboard or use them in automations. For example, you can send yourself a message when a baby hasn't had a feed in four hours.

## Supported devices

The integration doesn't use any hardware. It works with a Baby Tracker account, and every baby in the account becomes a {% term device %} in Home Assistant. Records logged on any phone signed in to the account show up in Home Assistant after the next update.

## Prerequisites

- A Baby Tracker account with the email address and password you use to sign in to the app.
- At least one baby added to the account in the app.

{% include integrations/config_flow.md %}

{% configuration_basic %}
Email:
    description: "The email address of your Baby Tracker account."
Password:
    description: "The password of your Baby Tracker account."
{% endconfiguration_basic %}

Home Assistant checks the email and password before it saves anything. The first time you set up the integration, Home Assistant loads your account's full history in one go, which can take a moment. Each Baby Tracker account can be added once.

Home Assistant signs in as its own device, named **Home Assistant**. It never borrows the identity of a phone.

### If your password changes

If Baby Tracker no longer accepts the saved password, Home Assistant asks you to sign in again. Enter your account's current password. The email address is the one you set up the integration with, and it isn't asked for again.

## Supported functionality

The **Baby Tracker (Nighp)** integration creates one device for each baby in your account. The device has the baby's name, and **Baby** when the baby has no name in the app. Nighp is the manufacturer, and the model is **Baby Tracker**.

### Sensors

Each baby has three sensors. Their state is the time the record happened, so a dashboard card can show "23 minutes ago" instead of a clock time. The state is unknown when the baby has no record of that kind. The attributes belong to the newest record, and an attribute is left out when the record has no value for it.

- **Last diaper**
  - **Description**: When the baby's newest diaper was logged.
  - **Attributes**: `type` (`wet`, `dirty`, or `mixed`) and `note`.
- **Last feed**
  - **Description**: When the baby's newest feed was logged. The newest of the nursing, formula, and pumped milk records counts as the last feed.
  - **Attributes**: `kind` (`nursing`, `formula`, or `pumped`) and `note`. For a bottle (formula or pumped milk), `amount` and `unit` (`fl. oz.` or `mL`). For nursing, `left_duration_minutes`, `right_duration_minutes`, `both_duration_minutes`, and `finish_side` (`left` or `right`).
- **Last sleep**
  - **Description**: When the baby's newest sleep was logged.
  - **Attributes**: `duration_minutes`, which is `0` while the sleep is still running, and `note`.

## Baby Tracker (Nighp) automation examples

These examples use a baby named Juniper, so the sensors are `sensor.juniper_last_feed` and `sensor.juniper_last_diaper`. The entity IDs in your Home Assistant follow your baby's name.

{% include docs/paste_yaml_tip.md %}

### Automation: Send a message when a baby hasn't had a feed in four hours

The state of **Last feed** changes each time a new feed appears. A state trigger with a duration therefore runs when four hours pass without a new feed.

- **Trigger**: State of an entity
  - **Entity**: Juniper Last feed
  - **For**: 4 hours
- **Action**: Send a notification message
  - **Target**: My Device (`notify.my_device`)

{% details "YAML example for sending a message after four hours without a feed" %}

{% example %}
automation: |
  alias: "Juniper has not had a feed in four hours"
  triggers:
    - trigger: state
      entity_id: sensor.juniper_last_feed
      for:
        hours: 4
  actions:
    - action: notify.send_message
      target:
        entity_id: notify.my_device
      data:
        message: "Juniper has not had a feed in four hours."
{% endexample %}

{% enddetails %}

### Automation: Send a message when a dirty diaper is logged

The **Last diaper** sensor has the diaper's `type` as an attribute. This example sends a message only when the newest diaper is a dirty one.

- **Trigger**: State of an entity
  - **Entity**: Juniper Last diaper
- **Condition**: Template
  - **Template**: The new diaper's `type` is `dirty`
- **Action**: Send a notification message
  - **Target**: My Device (`notify.my_device`)

{% details "YAML example for sending a message about a dirty diaper" %}

{% example %}
automation: |
  alias: "Juniper has a dirty diaper"
  triggers:
    - trigger: state
      entity_id: sensor.juniper_last_diaper
  conditions:
    - condition: template
      value_template: "{{ trigger.to_state.attributes.type | default('') == 'dirty' }}"
  actions:
    - action: notify.send_message
      target:
        entity_id: notify.my_device
      data:
        message: "A dirty diaper was logged for Juniper."
{% endexample %}

{% enddetails %}

## Data updates

The **Baby Tracker (Nighp)** integration {% term polling polls %} the Baby Tracker service every 5 minutes. The interval is fixed. Records logged on a phone show up in Home Assistant after the next update.

The first update loads your account's full history. Home Assistant saves what it has seen, so after a restart it fetches only what's new.

To check for new records sooner, use the `homeassistant.update_entity` action on any Baby Tracker sensor, for example in an automation or a script.

## Known limitations

- The integration only reads your account. It can't log diapers, feeds, or sleep. Use the Baby Tracker app for that.
- Records arrive on a schedule, not instantly. See [Data updates](#data-updates).
- Nighp doesn't offer a public API. The integration uses the same sync service as the app, which Nighp can change without notice. A change may stop the integration until it's updated.
- Each sensor shows only the newest record of its kind. The integration doesn't provide a history of every record.

## Troubleshooting

{% details "Can't set up the integration" %}

### Symptom: "The email or password is wrong."

When you set up the integration, the form shows the message "The email or password is wrong."

#### Description

Baby Tracker didn't accept the email address and password.

#### Resolution

1. Check that you can sign in to the Baby Tracker app with the same email address and password.
2. Enter them again in Home Assistant.

### Symptom: "Couldn't reach the Baby Tracker service. Check your connection and try again."

When you set up the integration, the form shows this message.

#### Description

Home Assistant couldn't connect to the Baby Tracker service.

#### Resolution

1. Check that your Home Assistant instance is connected to the internet.
2. Wait a few minutes and try again. The service may be down for a short time.

{% enddetails %}

{% details "Home Assistant asks you to sign in again" %}

### Symptom: Home Assistant asks you to sign in again

A message in **Settings** > **Devices & services** says the integration needs your attention.

#### Description

Baby Tracker rejected the saved password, usually because it changed.

#### Resolution

1. Go to **Settings** > **Devices & services**.
2. Follow the prompt on the **Baby Tracker (Nighp)** card.
3. Enter your account's current password.

{% enddetails %}

{% details "Sensors are unavailable or show old values" %}

### Symptom: Sensors are unavailable

The sensors show **Unavailable**.

#### Description

Home Assistant couldn't update from the Baby Tracker service, so the sensors are unavailable until an update works again.

#### Resolution

1. Check that your Home Assistant instance is connected to the internet.
2. Look in the Home Assistant log for the error. It names the reason the update failed.
3. If the error continues, [download the diagnostics](#download-diagnostics) and report it.

### Symptom: A sensor doesn't show a record you just logged

#### Description

Records logged on a phone arrive at the next update, within 5 minutes.

#### Resolution

Use the `homeassistant.update_entity` action on any Baby Tracker sensor to update right away.

{% enddetails %}

### Download diagnostics

To attach information to a bug report, go to **Settings** > **Devices & services**, select the **Baby Tracker (Nighp)** card, and select **Download diagnostics**. The file holds the integration's settings, the sync state, and counts of records. Your email address, password, and the device ID Home Assistant signs in with are redacted. It holds no baby's name or ID and no notes.

Report problems at [github.com/wtadler/ha-baby-tracker](https://github.com/wtadler/ha-baby-tracker/issues).

## Removing the integration

This integration follows standard integration removal. Deleting the integration also deletes the saved sync state.

{% include integrations/remove_device_service.md %}

### Removing a baby

When you delete a baby in the Baby Tracker app, Home Assistant removes the baby's device and sensors at the next update. If you add the baby back later, Home Assistant creates the device and sensors again.

If a baby's device is left behind, you can delete it from the device page. Select the three dots {% icon "mdi:dots-vertical" %} menu, and then select **Delete**. A baby that is still in your account can't be deleted, because the next update would add it back.
