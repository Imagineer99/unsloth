@echo off
rem Stand-in hipinfo for the Windows GPU decision matrix (ci_winmatrix).
rem install.ps1 and studio/setup.ps1 read "gcnArchName:" and "Name:" per device.
if defined FAKE_LOG >>"%FAKE_LOG%" echo hipinfo pid=cmd args=[%*]
echo.
echo --------------------------------------------------------------------------------
echo device#                           0
echo Name:                             AMD Radeon RX 9070 XT
echo pciBusID:                         3
echo pciDeviceID:                      0
echo pciDomainID:                      0
echo multiProcessorCount:              32
echo totalGlobalMem:                   15.92 GB
echo major:                            12
echo minor:                            0
echo gcnArchName:                      gfx1201
echo isIntegrated:                     0
echo.
exit /b 0
