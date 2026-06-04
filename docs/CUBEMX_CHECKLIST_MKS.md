# Чек-лист настройки CubeMX (.ioc) для МКС — STM32F411RETx

Сверьте свой `.ioc` с этим списком. Этап — bring-up, **БЕЗ FreeRTOS** и пока
можно **без USB** (USB добавим, когда дойдём до протокола; для чтения DEV_ID он
не нужен). Минимально нужны: тактирование, SPI2, SPI3, GPIO (CS/RST/IRQ).

## 1. Тактирование (RCC / Clock Configuration)
- [ ] RCC → High Speed Clock (HSE) = **Crystal/Ceramic Resonator** (кварц 24 МГц).
- [ ] Clock Config: HSE 24 МГц → PLL → **SYSCLK 84 МГц** (макс для F411 — 100 МГц,
      но 84 удобно и хватает).
- [ ] Если будете включать USB позже: настроить **PLL так, чтобы USB clock = 48 МГц**
      (поле "To USB" в Clock Configuration должно показывать 48 МГц). На 24 МГц HSE
      это достижимо; CubeMX подсветит красным, если 48 не получается.

## 2. SPI2 — модуль M1
- [ ] Mode: **Full-Duplex Master**.
- [ ] Hardware NSS: **Disable** (CS дёргаем вручную через GPIO, см. ниже).
- [ ] Data Size: 8 bits, MSB First.
- [ ] **CPOL = Low, CPHA = 1 Edge (SPI mode 0)** — стандарт для DW1000.
- [ ] Пины: **SCK=PB10, MISO=PC2, MOSI=PC3** (проверьте, что CubeMX назначил
      именно эти; при необходимости переназначьте кликом по пину).
- [ ] BaudRate: для bring-up поставьте такой прескалер, чтобы было **< 3 МГц**
      (например, /32 при 42 МГц APB1 → ~1.3 МГц). Быстрый режим включим из кода.

## 3. SPI3 — модуль M2
- [ ] Mode: **Full-Duplex Master**, NSS Disable, 8 bit, MSB First, SPI mode 0.
- [ ] Пины: **SCK=PC10, MISO=PC11, MOSI=PC12**.
      ВНИМАНИЕ: НЕ дефолтные PB3/PB4 (они — JTAG/SWO). Должно быть PC10/11/12.
- [ ] BaudRate: аналогично, медленно для bring-up.

## 4. GPIO — Chip Select (выход, push-pull, начальный уровень HIGH)
- [ ] **PB12** → GPIO_Output, имя метки CS1 (M1). Initial level: **High**.
- [ ] **PA15** → GPIO_Output, имя CS2 (M2). Initial level: **High**.
      (PA15 — это ещё и JTDI; на SWD не мешает. CubeMX может предупредить — ок.)

## 5. GPIO — RST (выход, push-pull)
- [ ] **PB6** → GPIO_Output (RST1 → M1). Initial level: уровень "не сброс"
      (зависит от полярности; поставьте Low, скорректируем после проверки OQ-9).
- [ ] **PB7** → GPIO_Output (RST2 → M2). Initial level: Low.

## 6. GPIO — IRQ (вход с прерыванием EXTI) — можно отложить до этапа RX
- [ ] **PB8** → GPIO_EXTI8 (IRQ1 ← M2). Trigger: Rising edge. Pull: Pull-down.
- [ ] **PB9** → GPIO_EXTI9 (IRQ2 ← M1). Trigger: Rising edge. Pull: Pull-down.
- [ ] NVIC: разрешить **EXTI line[9:5] interrupt**.
  (Для простого чтения DEV_ID прерывания НЕ нужны — этот пункт можно сделать позже.)

## 7. EEPROM M95080 — SPI1 (можно отложить)
- [ ] SPI1: Full-Duplex Master, пины PA5(SCK)/PA6(MISO)/PA7(MOSI).
- [ ] PA4 → GPIO_Output (CS EEPROM), Initial High.
  (Для bring-up радио не нужно; настроим, когда займёмся settings_storage.)

## 8. SYS / Debug
- [ ] SYS → Debug: **Serial Wire** (SWD). НЕ ставьте Trace, чтобы не занимать PB3.
- [ ] Timebase Source: **SysTick** (раз пока без FreeRTOS).

## 9. Настройка компилятора (Project → Properties → C/C++ Build → Settings)
- [ ] C/C++ Build → Settings → Tool Settings → MCU GCC Compiler → Preprocessor:
      добавить define **`DWT_NUM_DW_DEV=2`** и **`BOARD_MKS`**.
- [ ] Include paths: добавить
      `../App/board`, `../App/platform`, `../Drivers/decadriver`
      (и другие папки App/* по мере появления).
- [ ] Source location: убедиться, что `App` и `Drivers/decadriver` входят в сборку
      (обычно подхватываются автоматически как подпапки корня).

## 10. После генерации — в main.c (в секциях USER CODE!)
```c
/* USER CODE BEGIN Includes */
#include "deca_port.h"
#include "board_config.h"
extern int bringup_read_devids(void);
/* USER CODE END Includes */

/* ... после MX_SPI2_Init(); MX_SPI3_Init(); MX_GPIO_Init(); ... */

/* USER CODE BEGIN 2 */
int ok = bringup_read_devids();   /* поставить точку останова здесь */
(void)ok;                          /* смотреть g_devid[] в отладчике */
/* USER CODE END 2 */
```
Ожидаем: `g_devid[0] == 0xDECA0130` и `g_devid[1] == 0xDECA0130`.
Если читается `0x00000000` или `0xFFFFFFFF` — см. раздел "Диагностика" в чате
(частые причины: полярность RST, неверный пин CS/шина, скорость SPI, режим SPI).
