/*******************************************************************************
 *  МКС — Модуль коммуникации и сопряжения
 *  Прошивка для STM32F411 + 2x DWM1000 (DW1000)
 *
 *  Файл:     version.h
 *  Описание: Единый источник версии прошивки. Менять ТОЛЬКО здесь.
 *
 *  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
 *******************************************************************************/
#ifndef APP_VERSION_H
#define APP_VERSION_H

#define FW_VERSION_MAJOR   0
#define FW_VERSION_MINOR   1
#define FW_VERSION_PATCH   0

#define FW_VERSION_STR     "0.1.0"

/* Поддерживаемые версии протокола/API (для справки и ответов) */
#define MKS_PROTOCOL_VER   "1.3"
#define MKS_API_VER        "1.3"

#endif /* APP_VERSION_H */
