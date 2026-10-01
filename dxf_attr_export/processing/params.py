"""Мелочи, общие для обоих алгоритмов: параметры окна и проверка пути.

В окне видно только то, что нужно решить для верного результата; тонкая
настройка с разумными умолчаниями убрана под «Дополнительно», чтобы главное
помещалось без прокрутки.
"""

import os

from qgis.core import QgsProcessingParameterDefinition, QgsProcessingUtils


def add(algorithm, parameter, help_text=None, advanced=False):
    """Добавить параметр, повесив подсказку и, при надобности, флаг «дополнительно»."""
    if help_text:
        parameter.setHelp(help_text)
    if advanced:
        parameter.setFlags(parameter.flags()
                           | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
    algorithm.addParameter(parameter)
    return parameter


def is_temporary(path):
    """Файл попал во временную папку Processing и когда-нибудь исчезнет?"""
    if not path:
        return False
    temp = os.path.normpath(QgsProcessingUtils.tempFolder())
    return os.path.normpath(path).startswith(temp + os.sep)
