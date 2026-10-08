# First run

The first time you open the application, an assistant walks you through setup. Nothing is
changed on your computer until the step **What will change on this computer**, which lists
every change before you confirm.

| Step | What happens |
|---|---|
| Welcome | choose the language |
| How will you use this computer? | *Personal computer* or *Workstation* |
| Choose your work folder | default `Desktop/Work`; pick any other folder with the folder browser |
| What is available | which tools were found, and how to get the missing ones |
| Recommended setup *(Workstation)* | sync, shared keyboard and mouse, start at sign-in, integrations |
| What will change *(Workstation)* | the complete list of files and services, and the permissions needed |
| Pair another computer *(optional)* | pairing code and confirmation number |
| Home server *(optional)* | address, identity check, test |
| Cloud server *(optional)* | address, identity check, health check |
| System check | one row per thing you rely on, with *Fix automatically* where possible |
| Finish | open the application |

## Personal computer

Only two things are created: the application's own settings folder and your work folder (if
it does not exist yet). No shell file is edited, no shortcut is registered and nothing runs in
the background. You can add Workstation features later from the overview page.

## Workstation

Each optional item is a checkbox with an explanation. Items whose tool is not installed are
shown as unavailable, with the reason — they are never silently skipped or reported as working.

Every file the assistant touches is backed up first and can be restored from
[Recovery](recovery.md).

## Existing folders are safe

If the folder you choose already contains files, they are kept exactly as they are. System
folders, credential folders and your whole home folder can not be chosen as the work folder.

## Running the assistant again

**Settings → Advanced → Run the first-run assistant again** (switch on *Show advanced
settings* first). Your existing settings are kept and offered as the defaults.

Next: [Work folder](workspace.md)
