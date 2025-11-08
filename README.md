The link installs a .deb package which you can depackage using dpkg or sudo apt install commands. 'apt' is preferred since it also manages dependencies automatically.
Check out your Bluetooth configuration before trying to use the Applicaton.
Everything from different bluez versions, to experimental features enabled needs to be checked.
Only after enabling experimental features can you turn on bluetooth-mesh.service.
It is recommended you take AI support when configuring you system to run the application.


Since hardware/ kernel level configuration is not our scope, we do not try to mess with you system settings just to make our program work, but that also means if you hardware or firmware does not have the proper capabilities, it would require you to configure them yourself.

For testing, if required, an ESP32 can be provided.
