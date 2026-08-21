from abc import ABC,abstractmethod
from datetime import date
import pandas as pd
class DataProvider(ABC):
    @abstractmethod
    def stock_list(self)->pd.DataFrame: raise NotImplementedError
    @abstractmethod
    def history(self,code:str,start:date,end:date)->pd.DataFrame: raise NotImplementedError
