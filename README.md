# emporia_vue Home Assistant Integration

Reads data from the Emporia Vue energy monitor. Creates a sensor for each device channel showing average usage over each minute.

Note: This project is not associated with or endorsed by Emporia Energy.

Data is pulled from the Emporia API using the [PyEmVue python module](https://github.com/magico13/PyEmVue), also written by me.

![ha_example](images/ha_example.png)

## Installation with HACS

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

The simplest way to install this integration is with the Home Assistant Community Store (HACS). This is not (yet) part of the default store and will need to be added as a custom repository.

Setting up a custom repository is done by:

1. Go into HACS from the side bar.
2. Click into Integrations.
3. Click the 3-dot menu in the top right and select `Custom repositories`
4. In the UI that opens, copy and paste the [URL for this GitHub repository](https://github.com/babgvant/ha-emporia-vue) into the `Add custom repository URL` field.
5. Set the category to `Integration`.
6. Click the `Add` button.
7. Select Emporia Vue from the list and press the download button.
8. Further configuration is done within the Integrations configuration in Home Assistant. You may need to restart home assistant and clear your browser cache before it appears, try ctrl+shift+r if you don't see it in the configuration list.

![hacs1](images/hacs1.PNG)
![hacs2](images/hacs2.PNG)
![hacs3](images/hacs3.PNG)
![hacs4](images/hacs4.PNG)

## Manual Installation

If you don't want to use HACS or just prefer manual installs, you can install this like any other custom component. Just merge the `custom_components` folder with the one in your Home Assistant config folder and you may need to manually install the PyEmVue library.

## Configuration

Configuration is done directly in the Home Assistant UI, no manual config file editing is required.

1. Go into the Home Assistant `Configuration`
2. Select `Integrations`
3. Click the `+` button at the bottom
4. Search for "Emporia Vue" and add it. If you do not see it in the list, ensure that you have installed the integration.
5. In the UI that opens, enter the email and password used for the Emporia App. If your account uses Google/Apple, see the [Google/Apple Accounts](#googleapple-accounts) section below.
6. Done! You should now have a sensor for each "channel".

### Add circuits to the Energy Dashboard

In **Developer Tools > Actions**, run
`emporia_vue.add_to_energy_dashboard` and select an Emporia Vue monitor. The
action adds the monitor's eligible branch circuits, including circuits on its
nested monitors, to **Energy > Individual devices**. It skips panel totals,
balance/grid/solar channels, and circuits that are already configured. Existing
Energy Dashboard sources and settings are preserved.

Alternatively, open the integration's reconfigure page and select each physical
monitor under **Monitors whose circuits are added to Energy Dashboard**. This
explicit selection includes combined subpanels without relying on hierarchy
discovery and is reapplied safely whenever the integration loads.

Enable the integration's **Energy Today Sensor** option before using the action.
When the action is called with response data enabled, its result reports added,
already configured, aggregate-skipped, and incompatible circuit counts.

### Google/Apple Accounts

If your Emporia account was created via Sign in with Google or Apple, the easiest solution is to **set an Emporia password** using the create account flow on the Emporia website or app using the same email address as you'd use with Google/Apple. Once set, you can log in using the standard email and password method above.

If you are unable to set a password, the integration also supports token-based authentication. To obtain your tokens:

1. Open [web.emporiaenergy.com](https://web.emporiaenergy.com) in a browser and sign in with Google/Apple.
2. Open your browser's Developer Tools (F12) and go to the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox).
3. Under **IndexedDB** → **com.amplify.awsCognitoAuthPlugin** → **default.store**, look for keys ending in `.hostedUi.idToken`, `.hostedUi.accessToken`, and `.hostedUi.refreshToken` - copy the values of all three, making sure to only keep the values within the quotes (should start with `eyJ` or similar)
4. Use those values in the token authentication step of the integration setup.
